#!/usr/bin/env python

import base64
import configparser
import errno
import gzip
import hashlib
import io
import logging
import os
import shutil
import signal
import sys
import time
from logging import handlers
from argparse import ArgumentParser
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from urllib.error import HTTPError

import bencodepy
from bencodepy.exceptions import DecodingError
import feedparser
import sqlite3
import transmissionrpc
from pytvdbapi import api as tvdb_api
from pytvdbapi import error as tvdb_error


class UserError(Exception):
    pass

# constants
NAME = 'tvfetch'
DEFAULT_DB_PATH = os.path.join(sys.prefix, 'var/{}/db.sqlite'.format(NAME))
STATUS_COMPLETE = 'C'
STATUS_INCOMPLETE = 'I'
STATUS_SEEDING = 'S'
SHOW_DEFAULTS = {
    'quality': 'HDTV',
    'seed_ratio': 1,
    'start_season': 1,
    'start_episode': 1,
    'exclude_extensions': '',
    'max_concurrent': 2,
}
LOG_FORMAT = '%(levelname)s: %(message)s'
FILE_LOG_FORMAT = '%(asctime)s: ' + LOG_FORMAT

DATE_FORMAT = '%Y-%m-%d %H:%M:%S'
LOG_LEVELS = {
    'debug': logging.DEBUG,
    'info': logging.INFO,
    'warning': logging.WARNING,
    'error': logging.ERROR,
    'critical': logging.CRITICAL
}
DEFAULT_CONFIG_FILE = '/etc/%s.conf' % NAME

feed_url = 'http://ezrss.it/search/'

log = logging.getLogger('%s_log' % NAME)


class UserErorr(Exception):
    pass  # used for logging fatal user errors


class Config(object):
    def __init__(self, path):
        if not os.path.exists(path):
            example_cfg = os.path.join(
                sys.prefix, "{name}/{name}.conf.example")
            err = ("Config file not found: {}\n\nYou can find an example "
                   "configuration at {}".format(path, example_cfg))
            raise UserError(err)

        self.config = configparser.ConfigParser()
        self.config.read(path)

        try:
            self.defaults = self.config.items('defaults', 1)
        except:
            # create a dummy defaults section
            self.config.add_section('defaults')
            self.defaults = {}

    def get(self, section, key, default=None):
        try:
            val = self.config.get(section, key)
        except (configparser.NoOptionError, configparser.NoSectionError):
            val = default
        return hasattr(val, 'strip') and val.strip('"') or val

    def items(self, section, defaults={}):
        result = defaults.copy()
        try:
            data = self.config.items(section, 1)
        except configparser.NoSectionError:
            data = {}
        result.update(data)
        # strip quotes from values
        unquote = lambda v: v.strip('"') if hasattr(v, 'strip') else v
        result = dict([(k, unquote(v)) for k, v in result.items()])
        return result

    def sections(self):
        return self.config.sections()


class TvFetch(object):

    def __init__(self, configfile):
        # load config
        try:
            self.config = Config(configfile)
        except configparser.Error as e:
            raise UserError('Could not parse config file: %s' % e)

        # setup logging
        log.setLevel(logging.INFO)

        # file-based log
        log_filename = self.config.get('daemon', 'log_file')
        if log_filename:
            rotating_handler = handlers.RotatingFileHandler(
                log_filename, maxBytes=1048576, backupCount=5)
            rotating_handler.setFormatter(
                logging.Formatter(FILE_LOG_FORMAT, DATE_FORMAT))
            log.addHandler(rotating_handler)

        # console logging
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter(LOG_FORMAT))
        log.addHandler(ch)

        # Set log level from config
        log_level = LOG_LEVELS.get(
            self.config.get('daemon', 'log_level', 'info'))

        log.setLevel(log_level)

        log.info('Running {}'.format(NAME))
        # TVDB API
        tvdb_api_key = self.config.get('tvdb', 'api_key')
        if not tvdb_api_key:
            raise UserError("You must set the api_key setting in the "
                            "[tvdb] section. Get an API key at "
                            "http://thetvdb.com/")
        self.tvdb = tvdb_api.TVDB(tvdb_api_key)
        self.tvdb_lang = self.config.get('tvdb', 'language', 'en')

        # setup database if needed
        db_dir = os.path.dirname(DEFAULT_DB_PATH)
        if not os.path.exists(db_dir):
            os.makedirs(db_dir, 0o770)
        db_path = self.config.get('daemon', 'db_path', DEFAULT_DB_PATH)
        db_exists = os.path.exists(db_path)
        self.db = sqlite3.connect(db_path)
        if not db_exists:
            c = self.db.cursor()
            c.execute(
                'create table shows(name text, season integer, '
                'episode integer, title text, status text, url text, '
                'transid integer, cfg_name text)')
            log.debug('Created initial database')

    @property
    def transmission(self):
        if not hasattr(self, '_transmission_client'):
            trans_cfg = self.config.items('transmission', {
                'host': 'localhost',
                'port': '9091',
                'user': None,
                'password': None
            })

            trans_cfg['address'] = trans_cfg.pop('host')

            self._transmission_client = transmissionrpc.Client(**trans_cfg)

        return self._transmission_client

    def find_new(self):
        # find our shows
        builtins = ('daemon', 'transmission', 'defaults', 'tvdb')
        shows = [s for s in self.config.sections() if s not in builtins]
        defaults = self.config.items('defaults', SHOW_DEFAULTS)

        for cfg_name in shows:
            show = self.config.items(cfg_name, defaults)
            show['name'] = show.get('name', cfg_name)
            show['feed_search'] = show.get('feed_search', show['name'])
            log.debug('Looking up %s' % show['feed_search'])

            # if we're at downloading max_concurrent episodes, then stop
            # processing this show
            max_concurrent = int(show.get('max_concurrent'))
            c = self.db.cursor()
            c.execute(
                'select count(*) from shows where cfg_name=? and status=?',
                (cfg_name, STATUS_INCOMPLETE)
            )
            count = c.fetchone()[0]
            if count >= max_concurrent:
                log.debug(
                    'Reached maximum concurrent torrents (%d) for "%s".' % (
                        max_concurrent, cfg_name)
                )
                continue

            # Get the show data from TVDB
            if show.get('tvdb_id'):
                try:
                    tvdb_show = self.tvdb.get(show['tvdb_id'], self.tvdb_lang)
                except tvdb_error.TVDBIdError:
                    result = []
            else:
                result = self.tvdb.search(show['name'], self.tvdb_lang)
                if not len(result):
                    log.error('Show not found on tvdb: %s' % show['name'])
                    continue

                if len(result) > 1:
                    log.warning('Multiple matches found for "{r.search}"'
                                .format(r=result))

                tvdb_show = result[0]
            try:
                # if show has a season 0 (extras), don't count it in the total
                # number of seasons.
                tvdb_show[0]
            except tvdb_error.TVDBIndexError:
                num_seasons = len(tvdb_show)
            else:
                num_seasons = len(tvdb_show) - 1

            if num_seasons <= 0:
                log.error('No seasons found for "{r.search}"'.format(r=result))
                continue

            # Get last downloaded season from the database to see which season
            # to start with. If no downloads, use start_season
            c = self.db.cursor()
            c.execute(
                'select max(season) from shows where cfg_name=?', (cfg_name,))
            max = c.fetchone()[0]
            start_season = max or show['start_season']

            # load torrent feeds one season at a time, since the feed only
            # returns a max of 30 shows.
            entries = []
            for season in range(int(start_season), num_seasons + 1):
                # load rss feed:
                feed_params = {
                    'mode': 'rss',
                    'show_name': show['feed_search'],
                    'quality': show.get('quality'),
                    'season': season
                }
                if show.get('feed_search_exact', 'false').lower() != 'false':
                    feed_params['show_name_exact'] = 'true'

                show_feed_url = feed_url + '?' + urlencode(feed_params)
                log.debug('checking feed url: %s' % show_feed_url)
                feed = feedparser.parse(show_feed_url)

                log.debug('found %d entries for %s, season %s' %
                          (len(feed['entries']), show['name'], season))

                # assume that feed has given episodes sorted by seed quality,
                # and maintain that order.
                for i, e in enumerate(feed['entries']):
                    feed['entries'][i]['ord'] = i

                # sort feed entries by episode
                def ordkey(ep):
                    summary = self._parse_summary(ep['summary'])
                    return summary['episode'] * 100 + ep['ord']
                entries += sorted(feed['entries'], key=ordkey)

                eps = [self._parse_summary(e['summary'])['episode']
                       for e in entries]
                log.debug('   Found episodes: {}'
                          .format(str([int(s) for s in set(eps)])))

            added = 0
            for entry in entries:
                if count >= max_concurrent:
                    log.info(
                        'Reached maximum concurrent torrents (%d) for this '
                        'show "%s".' % (max_concurrent, cfg_name))
                    break

                link = entry['link']
                summary = entry['summary']

                # parse summary details (assuming ezrss keeps this consistent)
                # ex: 'Show Name: Dexter; Episode Title: My Bad; Season: 5;
                # Episode: 1'
                info = self._parse_summary(summary)
                log.debug(
                    'Found: %(show_name)s: Season: %(season)s; '
                    'Episode: %(episode)s; Title: %(title)s' % info
                )

                season = int(info['season'])
                episode = int(info['episode'])

                # skip if less than start_episode. eg, s04e06 would be 406
                e2n = lambda s, e: int(s) * 100 + int(e)
                start_ssn = show['start_season']
                start_ep = show['start_episode']
                if (e2n(season, episode) < e2n(start_ssn, start_ep)):
                    log.debug(
                        'Skipping, s%02de%02d is earlier than start_episode'
                        % (season, episode))
                    continue

                # Check and see if we need this episode
                c = self.db.cursor()
                c.execute(
                    'SELECT COUNT() FROM shows WHERE cfg_name=? '
                    'AND season=? AND episode=?', (cfg_name, season, episode)
                )
                if c.fetchone()[0] > 0:
                    # already have this one, or are already downloading it.
                    log.debug(
                        '"%(show_name)s-%(season)s-%(episode)s" has already '
                        'been downloaded or is currently downloading' % info
                    )
                    continue

                # Get torrent file so that we can parse info out of it
                log.debug('Decoding torrent...')
                try:
                    request = Request(link)
                    request.add_header('Accept-encoding', 'gzip')
                    response = urlopen(request)
                except HTTPError as e:
                    log.debug('Could not download torrent: %s, %s' % (link, e))
                    continue

                if response.info().get('Content-Encoding') == 'gzip':
                    buf = io.BytesIO(response.read())
                    f = gzip.GzipFile(fileobj=buf)
                    data = f.read()
                else:
                    data = response.read()

                try:
                    torrent = bencodepy.decode(data)
                except DecodingError as e:
                    log.debug(str(e))
                    log.error('Could not parse torrent: %s' % link)
                    continue

                filename = torrent[b'info'].get(b'name').decode()
                if not filename:
                    files = torrent[b'info'][b'files']
                    # get largest file
                    files = sorted(
                        files, key=lambda f: f['length'], reverse=True)
                    filename = files[0]['path']

                ext = os.path.splitext(filename)[1][1:]
                if ext in show['exclude_extensions'].split(','):
                    log.debug(
                        'Skipping %s, file extension blacklisted' % filename)
                    continue

                # Add the show
                added += 1
                log.info(
                    'Adding %(show_name)s-%(season)s-%(episode)s to '
                    'transmission queue' % info)
                log.debug(link)
                b64_data = base64.b64encode(data).decode()
                try:
                    trans_info = self.transmission.add_torrent(b64_data)
                except transmissionrpc.error.TransmissionError as e:
                    if '"duplicate torrent"' in str(e):
                        log.info('Torrent already exists. Resuming.')
                        # TODO: Find the duplicate torrent
                        binfo = bencodepy.encode(torrent[b'info'])
                        hash = hashlib.sha1(binfo)
                        trans_info = self.transmission.inf(hash.hexdigest())
                        self.transmission.start(trans_info.id)
                    else:
                        raise

                # Record in db
                c = self.db.cursor()
                show_name = tvdb_show.SeriesName
                try:
                    title = tvdb_show[season][episode].EpisodeName
                except (tvdb_error.TVDBIndexError, KeyError):
                    title = info.get('title', '(no title)')
                c.execute(
                    'INSERT INTO shows (name, season, episode, title, status, '
                    'url, transid, cfg_name) VALUES (?, ?, ? ,? ,? ,?, ?, ?)',
                    (show_name, season, episode, title, STATUS_INCOMPLETE,
                     link, trans_info.id, cfg_name)
                )
                self.db.commit()
                count += 1

            if added == 0:
                log.info('No new episodes found for %s' % cfg_name)

    def check_progress(self):
        log.debug('Checking progress')

        defaults = self.config.items('defaults', SHOW_DEFAULTS)

        # Check for removed torrents
        c = self.db.cursor()
        try:
            c.execute(
                'SELECT name, season, episode, title, status, url, transid, '
                'cfg_name FROM shows WHERE status=? OR status=?',
                (STATUS_INCOMPLETE, STATUS_SEEDING)
            )
        except sqlite3.InterfaceError as e:
            # TODO: Not sure why this happens yet, seems random.
            return

        for row in c:
            (show_name, season, episode, title, status, url, transid,
             cfg_name) = row
            show_cfg = self.config.items(cfg_name, defaults)
            seed_ratio = float(show_cfg.get('seed_ratio', 1))

            try:
                torrent = self.transmission.info(transid)[transid]
            except KeyError:
                # Torrent was removed, so remove from our db
                c2 = self.db.cursor()
                c2.execute('DELETE FROM shows WHERE transid=?', (transid,))
                self.db.commit()
                log.info('Torrent removed: %s' % url)
            else:
                download_dir = torrent._fields['downloadDir'].value
                # otherwise, check the status
                if status == STATUS_INCOMPLETE and torrent.progress == 100:
                    # The largest file is likely the one we want.
                    files = torrent.files().values()
                    sortkey = lambda f: f['size']
                    files = sorted(files, key=sortkey, reverse=True)
                    file = files[0]['name']
                    if not show_cfg.get('destination'):
                        raise UserError(
                            'destination not found for show "%s"' % cfg_name)
                    # Move the file to its final destination
                    destination = show_cfg['destination'] % {
                        'show_name': show_name,
                        'season': season,
                        'episode': episode,
                        'title': title,
                    }
                    ext = os.path.splitext(file)[1]
                    destination += ext
                    # Make sure target directory exists
                    try:
                        os.makedirs(os.path.dirname(destination))
                    except OSError as e:
                        if e.errno != errno.EEXIST:
                            raise
                    # if we don't need the file around for seeding, moving is
                    # faster
                    if torrent.ratio >= seed_ratio:
                        shutil.move(
                            os.path.join(download_dir, file), destination)
                    else:
                        shutil.copy(
                            os.path.join(download_dir, file), destination)

                    # Make sure torrent is seeding
                    self.transmission.start(transid)
                    c2 = self.db.cursor()
                    c2.execute(
                        'UPDATE shows SET status=? WHERE transid=?',
                        (STATUS_SEEDING, transid))
                    self.db.commit()
                    status = STATUS_SEEDING
                    log.info('Saved %s-s%02de%02d to %s' %
                             (show_name, season, episode, destination))

                elif (status == STATUS_INCOMPLETE
                      and torrent.status == 'stopped'
                      and not show_cfg.get('paused')):
                    log.info('Resuming %s-s%02de%02d' %
                             (show_name, season, episode))
                    self.transmission.start(transid)

                if status == STATUS_SEEDING and torrent.ratio >= seed_ratio:
                    c2 = self.db.cursor()
                    c2.execute(
                        'UPDATE shows SET status=? WHERE transid=?',
                        (STATUS_COMPLETE, transid))
                    self.db.commit()
                    log.info('Stopping torrent %s-s%02de%02d' %
                             (show_name, season, episode))
                    # stop the torrent
                    self.transmission.stop(transid)

                    # Clean up remaining files
                    log.debug('Cleaning up...')
                    files = []
                    dirs = []
                    for num, file in torrent.files().items():
                        file = file['name']
                        # The file path should be relative, but we'll do this
                        # to be safe.
                        if file.startswith('/'):
                            continue
                        # also for safetey
                        file = file.replace(download_dir, '')

                        parts = file.split('/')
                        _dirs = [d for d in parts[:-1] if d not in dirs]
                        dirs += _dirs
                        files.append(parts[-1])

                    for file in files:
                        log.debug('Deleted %s' % file)
                        try:
                            os.remove(os.path.join(download_dir, file))
                        except OSError as e:
                            if e.errno != errno.ENOENT:
                                # if it's alread deleted, we don't care
                                raise

                    for dir in dirs:
                        log.debug('Deleted %s' % dir)
                        if dir:  # safety
                            shutil.rmtree(os.path.join(download_dir, dir))

                    self.transmission.remove(transid)

    def reset_show(self, show):
        # load coonfig
        if show not in self.config.sections():
            raise UserError("Show does not exist: %s" % show)
        c = self.db.cursor()
        c.execute('DELETE FROM shows WHERE cfg_name=?', (show,))
        self.db.commit()
        log.info('Successfully deleted history for show "%s"' % show)

    def list_languages(self):
        for lang in tvdb_api.languages():
            print('{l.abbreviation}: {l.name}'.format(l=lang))

    def run_daemon(self):
        # try to guess a good pidfile location
        self.pidfile = self.config.get('daemon', 'pid_file', 'auto')
        if self.pidfile != 'auto':
            self.pidfile = self.config.get('daemon', 'pid_file')
        elif os.path.isdir('/run'):
            self.pidfile = '/run/{}.pid'.format(NAME)
        elif os.path.isdir('/var/run'):
            self.pidfile = '/var/run/{}.pid'.format(NAME)
        else:
            self.pidfile = '/tmp/{}.pid'.format(NAME)

        pid = str(os.getpid())
        if os.path.isfile(self.pidfile):
            print("already running ({})".format(self.pidfile))
            sys.exit()
        else:
            open(self.pidfile, 'w').write(pid)

        signal.signal(signal.SIGINT, self.handle_signal)

        # time interval to check for new shows, defaults to 30 minutes
        check_seconds = self.config.get('daemon', 'check_time', 30) * 60

        last_check = None
        while True:
            self.check_progress()
            now = time.time()
            if last_check is None or (now - last_check) > check_seconds:
                last_check = now
                try:
                    self.find_new()
                except transmissionrpc.error.TransmissionError as e:
                    if "Connection refused" in str(e):
                        log.error("Could not connect to transmission")
                    else:
                        raise
            time.sleep(5)

        os.unlink(self.pidfile)

    def handle_signal(self, sig, frame):
        print('\nCaught signal: {}'.format(str(sig)))
        self.shutdown()

    def shutdown(self):
        log.info('Shutting down')
        try:
            os.unlink(self.pidfile)
        except FileNotFoundError:
            pass
        sys.exit(0)

    def _parse_summary(self, summary):
        summary_data = dict([i.split(': ') for i in summary.split('; ')])
        info = {
            'show_name': summary_data.get('Show Name'),
            'season': int(summary_data.get('Season')),
            'episode': int(summary_data.get('Episode')),
            'title': summary_data.get('Episode Title'),
        }
        return info


def main():
    # parse options
    parser = ArgumentParser(description="TV torrent episode downloader")
    parser.add_argument('-c', '--config', action='store', metavar='FILE',
                        default=DEFAULT_CONFIG_FILE,
                        help="Path to configuration file")
    parser.add_argument('--reset-show', action='store', metavar='NAME',
                        help="Delete a show's download history and exit")
    parser.add_argument('--list-langauges', action='store_true',
                        dest='list_languages',
                        help="Show list of available languages and exit")
    options = parser.parse_args()

    try:
        fetcher = TvFetch(options.config)
        if options.reset_show:
            fetcher.reset_show(options.reset_show)
            sys.exit(0)

        elif options.list_languages:
            fetcher.list_languages()
            sys.exit(0)

        fetcher.run_daemon()
        sys.exit(0)

    except UserError as e:
        log.error(e)

    except Exception as e:
        log.exception(e)
        if fetcher:
            fetcher.shutdown()

    finally:
        sys.exit(1)


if __name__ == "__main__":
    main()
