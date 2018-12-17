# -*- coding: utf-8 -*-
"""
Usage:
  trackupdates.py <yaml> [--test] [--runjobs=<runjobs>] [--log=<level>]
  trackupdates.py (-h | --help)
  trackupdates.py --version

Options:
  -h --help     Show this screen.
  --version     Show version.
  --log=<level>    log level [default: INFO].
  --test        Test parse webpage content in local.
  --runjobs=<runjobs>    Specify job name with comma, default run all jobs [default: ].
"""
from docopt import docopt
import yaml
import logging
import utils
from datetime import datetime, timedelta
import database
import server
import random
logging.basicConfig()
logger = logging.getLogger(__file__)


class Settings:
    def __init__(self, path):
        logger.info('yaml config file path: %s' % path)
        self.path = path
        with open(path, 'r') as stream:
            self.yml_dict = yaml.load(stream)
        for k, v in self.yml_dict.items():
            setattr(self, k, v)
        for k, v in self.yml_dict['global'].items():
            setattr(self, k, v)

    def __getitem__(self, key):
        return self.yml_dict[key]

    def get_job(self, name):
        for c in self.yml_dict['jobs']:
            if c['name'] == name:
                return self._complete_job_config(c)

    def _complete_job_config(self, config):
        config['parser_config'] = self.get_parser(config['parser'])
        if 'update' not in config or config['update'] is None:
            config['update'] = {}
        if 'cron' not in config:
            config['cron'] = '*|%d' % random.randint(0, 59)
        rec_name = config['update'].get('receiver', '')
        config['update']['emails'] = self.get_receivers(rec_name)
        return config

    def get_receivers(self, name):
        for r in self.yml_dict['receivers']:
            if r['name'] == name:
                return r['email_configs']['to']
        return []

    def get_daily_report_receivers(self):
        return self.get_receivers(self.yml_dict['global'].get('daily_report_receivers', ''))

    def get_all_job_configs(self):
        for config in self.yml_dict['jobs']:
            job = self._complete_job_config(config)
            yield job

    def get_parser(self, name):
        for p in self.yml_dict['parsers']:
            if p['name'] == name:
                return p

    def get_parser_format(self, name):
        parser = self.get_parser(self.get_job(name)['parser'])
        return parser['format']


def new_mailer_from_settings(settings):
    glob = settings['global']
    if 'smtp_smarthost' not in glob:
        return None

    smtp = glob.get('smtp_smarthost').split(':')
    user = glob.get('smtp_auth_username', '')
    fromaddr = glob.get('smtp_from', user)
    passwd = glob.get('smtp_auth_password', '')

    return utils.Email(fromaddr, (smtp[0], int(smtp[1])), (user, passwd))


class Parser:
    def __init__(self, config):
        self.config = config
        self.base_url = config['base_url']

    def parse(self, content):
        items = []
        dom = utils.transfer2dom(content)
        base_xpath = self.config['base_xpath']
        for bx in base_xpath:
            for ele in dom.xpath(bx):
                items.append(self._parse_item(ele))
        return items

    def _parse_item(self, ele):
        d = {}
        for k, v in self.config['attr'].items():
            res = utils.get_xpath(ele, v)
            if hasattr(res, 'itertext'):
                res = ' '.join([r.strip() for r in res.itertext()])
            elif hasattr(res, 'text'):
                res = res.text()

            res = unicode(res).strip()
            if k.endswith('url') and not res.startswith('http'):
                res = self.base_url.rstrip('/') + '/' + res.lstrip('/')
            d[k] = res
        d['_crawl_time'] = datetime.now()
        return d


class Downloader:
    def __init__(self):
        pass

    def get(self, url, param, retry=3):
        return utils.get_data(url, param, retry)


class ListCrawl:
    def __init__(self, config, test=False):
        self.config = config
        self.test = test
        self.pconfig = config['parser_config']
        self.parser = Parser(self.config['parser_config'])
        self.downloader = Downloader()

    def _load_content(self):
        self.url_format = self.config['url']['test_target'] if self.test else self.config['url']['target']
        logger.info('Crawl content from format: ' + self.url_format)
        if not self.url_format.startswith('http'):
            yield utils.read_content(self.url_format)
            return

        query = self.config['url'].get('query_parameter', {})
        if len(query) == 0:
            yield self.downloader.get(self.url_format, {})
            return

        # TODO: Now only support one query parameter with enumerate value
        for k, v in query.items(): 
            for qp in v:
                values_list = []
                if qp['type']== 'string':
                    values_list = str(qp['value']).split(',')
                elif qp['type'] == 'distinct':
                    table = qp.get('table', self.config['name'])
                    col = qp['value']
                    job = self.sched.get_job(table)
                    for v in job.store.distinct(col):
                        if v[0]:
                            values_list.append(v[0])
                for v in values_list:
                    d = {k: v}
                    url = self.url_format.format(**d)
                    logger.info('Crawl content url: ' + url)
                    yield self.downloader.get(url, {})

    def run(self, sched=None):
        self.sched = sched
        items = []
        for c in self._load_content():
            items.extend(self.parser.parse(c))
        return items


class Job:
    def __init__(self, config, db, mailer=None, test=False):
        self.config = config
        self.db = db
        self.mailer = mailer
        self.name = self.config['name']
        self.crawl = ListCrawl(config, test)
        self.receivers = self.config['update']['emails']
        self.match = self.config['update'].get('match', {})
        self.filter_funcs = []
        for k, v in self.match.items():
            self.filter_funcs.append(keyword_contains(k, v))
        self.test = test
        self.col_map = config['parser_config']['attr']
        self.fmt = config['parser_config'].get('format', {})
        self._init_store()

    def _init_store(self):
        tname = self.name
        if self.test:
            tname = 'test_' + self.name
        self.item_class = self.db.create_table_if_not_exists(tname, self.col_map.keys(), self.fmt)
        self.store = database.Table(self.db, self.item_class, ['url'])

    def run(self, sche=None, filterbykeyword=True):
        logger.info('[%s]: job run' % self.name)
        items = self.crawl.run(sche)
        update = []
        for i in items:
            t = self.item_class(**i)
            # TODO: Need a simple and efficient method, now use attribute url
            # for default distinguish value
            key = t.url
            if self.store.get(key) is None:
                t = self.store.set(key, t)
                update.append(t)

        if filterbykeyword:
            update = filter(self._filter, update)
        logger.info('[%s]: Track updates: %d' % (self.name, len(update)))
        if not self.test and len(update):
            self.send_mail(update)
        return update

    def _filter(self, item):
        if len(self.filter_funcs) == 0:
            return True
        return any(map(lambda f: f(item), self.filter_funcs))

    def send_mail(self, update_items, head="New Update From", receivers=[]):
        if self.mailer is None:
            logger.warn('[%s]: email not config' % self.name)
            return
        if len(receivers) == 0:
            receivers = self.receivers

        if len(receivers) == 0:
            logger.warn('[%s]: receiver not specified' % self.name)
            return
        if len(update_items) == 0:
            return

        logger.info('[%s]: Send mail update count %d' % (self.name, len(update_items)))
        html = utils.markdown2html(update_items)
        subject = '%s [%s]' % (head, self.name)
        self.mailer.send(receivers, subject, html, fmt='html')


def keyword_contains(k, v):
    vlist = v.split(',')

    def c(item):
        for v in vlist:
            iattr = getattr(item, k)
            logger.debug('%s: %s' % (v, iattr))
            if v.lower() in iattr.lower():
                return True
        else:
            return False
    return c


def print_items(items):
    for i in items:
        logger.info(i)


class Scheduler:
    def __init__(self, config_path, blocking=True, test=False, runjobs=None):
        self.settings = Settings(config_path)
        self.blocking = blocking
        if blocking:
            from apscheduler.schedulers.blocking import BlockingScheduler
            self.sched = BlockingScheduler()
        else:
            from apscheduler.schedulers.background import BackgroundScheduler
            self.sched = BackgroundScheduler()
        self.test = test
        self.runjobs = runjobs.split(',') if type(runjobs) == str and len(runjobs) else []
        self.jobs = {}
        self.mailer = new_mailer_from_settings(self.settings)
        self.db = database.Database(self.settings['global'].get('store', 'track.db'))
        self._init_job()
        self.sched.add_job(self.daily_report, 'cron', **parse_job_cron('18|30'))

    def get_job(self, name):
        return self.jobs[name]

    def daily_report(self):
        receivers = self.settings.get_daily_report_receivers()
        if len(receivers) == 0:
            return
        for k, job in self.jobs.items():
            items = job.store.iter(starttime=datetime.now()-timedelta(days=1), num=50)
            job.send_mail(items, head="Daily Report From", receivers=receivers)

    def add_job(self, *args, **kws):
        self.sched.add_job(*args, **kws)

    def _init_job(self):
        for config in self.settings.get_all_job_configs():
            name = config['name']
            if len(self.runjobs) and name not in self.runjobs:
                continue
            job = Job(config, self.db, self.mailer, self.test)
            self.jobs[name] = job
            cron = parse_job_cron(config['cron'])
            self.sched.add_job(job.run, 'cron', [self], **cron)

    def run(self):
        try:
            if self.blocking or self.test:
                for k, job in self.jobs.items():
                    updates = job.run()
                    if self.test:
                        print_items(updates)
            if not self.test:
                self.sched.start()
        except (KeyboardInterrupt, SystemExit):
            self.sched.shutdown()


def parse_job_cron(cron_str):
    d = {}
    d['hour'], d['minute'] = cron_str.split('|')
    return d


__version__ = '0.0.9'


def main():
    args = docopt(__doc__, version=__version__)
    logger = logging.getLogger()
    logger.setLevel(getattr(logging, args['--log'].upper()))

    settings = Settings(args['<yaml>'])
    sched = Scheduler(args['<yaml>'], test=args['--test'], runjobs=args['--runjobs'], blocking=False)
    sched.run()
    server.Server(sched).run()

if __name__ == '__main__':
    main()
