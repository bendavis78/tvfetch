#!/usr/bin/env python

from logging import handlers
import ConfigParser
import errno
import hashlib
import logging
from optparse import OptionParser, make_option
import os
import shutil
import sys
import urllib2

from bzrlib import bencode
import feedparser
import sqlite3
import transmissionrpc
from textwrap import dedent
import tvdb_api

class UserError(Exception): pass

#constants
NAME = 'tvfetch'
DB = '/var/%s/db' % NAME
STATUS_COMPLETE='C'
STATUS_INCOMPLETE='I'
STATUS_SEEDING='S'
SHOW_DEFAULTS = {
    'quality': 'HDTV',
    'seed_ratio': 1,
    'start_season': 1,
    'start_episode': 1,
    'exclude_extensions': '',
    'max_concurrent': 2,
}
LOG_FILENAME='/var/log/%s/%s.log' % (NAME,NAME)
LOG_FORMAT='%(asctime)s: %(levelname)s: %(message)s'
DATE_FORMAT='%Y-%m-%d %H:%M:%S'
LOG_LEVELS = {
    'debug': logging.DEBUG,
    'info': logging.INFO,
    'warning': logging.WARNING,
    'error': logging.ERROR,
    'critical': logging.CRITICAL
}
CONFIG_FILE = '/etc/%s.conf' % NAME

feed_url = 'http://ezrss.it/search/?mode=rss&show_name=%(show_name)s&quality=%(quality)s&season=%(season)s'

# setup logging
log = logging.getLogger('%s_log' % NAME)
log.setLevel(logging.INFO)

# rotated log
handler = handlers.RotatingFileHandler(LOG_FILENAME, maxBytes=1024, backupCount=5)

handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
log.addHandler(handler)

# console output
ch = logging.StreamHandler()
ch.setFormatter(logging.Formatter('%(levelname)s: %(message)s', DATE_FORMAT))
log.addHandler(ch)

# load config

class UserErorr(Exception): pass # used for logging fatal user errors

class Config(object):
    def __init__(self):
        if not os.path.exists(CONFIG_FILE):
            raise UserError("Config file not found: %s" % CONFIG_FILE)
        self.config = ConfigParser.ConfigParser()
        self.config.read(CONFIG_FILE)
        try:
            self.globals = self.config.items('globals', 1)
        except:
            #create a dummy globals section
            self.config.add_section('globals')
            self.globals = {}
    
    def get(self, section, key, default=None):
        try:
            val = self.config.get(section, key)
        except ConfigParser.NoOptionError:
            val = default
        return val.strip('"')

    def items(self, section, defaults={}):
        result = defaults.copy()
        try:
            data = self.config.items(section, 1)
        except ConfigParser.NoSectionError:
            data = {}
        result.update(data)
        # strip quotes from values
        unquote = lambda v: v.strip('"') if hasattr(v, 'strip') else v
        result = dict([(k, unquote(v)) for k,v in result.iteritems()])
        return result

    def sections(self):
        return self.config.sections()


class TvFetch(object):
    def __init__(self):
        log.debug('Running...')
        # load coonfig
        try:
            self.config = Config()
        except ConfigParser.Error as e:
            raise UserError('Could not parse config file: %s' % e)
        
        # Set log level from config
        log_level = LOG_LEVELS.get(self.config.get('globals', 'log_level', 'info'))
        log.setLevel(log_level)

        log.debug('Loaded config')

        # setup database if needed
        db_exists = os.path.exists(DB)
        self.db = sqlite3.connect(DB)
        if not db_exists:
            c = self.db.cursor()
            c.execute('create table shows(name text, season integer, episode integer, title text, status text, url text, transid integer, cfg_name text)')
            log.debug('Created initial database')
        

        #get transmission client
        trans_cfg = self.config.items('transmission', {'host':'localhost','port':'9091'})
        trans_cfg['address'] = trans_cfg.pop('host')
        try:
            self.trans_client = transmissionrpc.Client(**trans_cfg)
        except RuntimeError as e:
            # in python 2.6.5, recursion occurs on wrong authentication
            # See http://bugs.python.org/issue8797
            if "maximum recursion" in str(e):
                msg = """

                A recursion error was detected when connecting to transmission. If 
                you are running python 2.6.5, this could be caused by a bug that 
                occurs when HTTP authentication fails. Please check your 
                transmission settings in %s.conf and try again.

                See http://bugs.python.org/issue8797 for more details.
                """ % NAME
                msg = dedent(msg)
                raise UserError(msg)
            raise

    def check_new(self):
        #find our shows
        shows = [s for s in self.config.sections() if s not in ('transmission', 'globals')]
        globals = self.config.items('globals', SHOW_DEFAULTS)
        
        for cfg_name in shows:
            show = self.config.items(cfg_name, globals)
            show['name'] = show.get('name', cfg_name)
            log.debug('Looking up %s' % show['name'])


            # if we're at downloading max_concurrent episodes, then stop processing this show
            max_concurrent = int(show.get('max_concurrent'))
            c = self.db.cursor()
            c.execute('select count(*) from shows where cfg_name=? and status=?', (cfg_name, STATUS_INCOMPLETE))
            count = c.fetchone()[0]
            if count >= max_concurrent:
                log.debug('Reached maximum concurrent torrents (%d) for "%s".' % (max_concurrent, cfg_name))
                continue

            #we need to figure out the end season for this show. Use tvdb.
            tvdb = tvdb_api.Tvdb()
            tvdb_show = tvdb[show['name']]
            num_seasons = len(tvdb_show.items())-1 #exclude season 0 (extras)

            # get last downloaded season from the database to see which season to start with.
            # if no downloads, use start_season
            c = self.db.cursor()
            c.execute('select max(season) from shows where cfg_name=?', (cfg_name,))
            max = c.fetchone()[0]
            start_season = max or show['start_season']
            
            #load torrent feeds one season at a time, since the feed only returns a max of 30 shows.
            entries=[]
            for season in range(int(start_season), num_seasons+1):
                #load rss feed:
                feed = feedparser.parse(feed_url % {'show_name': show['name'], 'quality': show.get('quality'), 'season':season})
                log.debug('found %d entries for %s, season %s' % (len(feed['entries']), show['name'], season))
                #assume that feed has given episodes sorted by seed quality, and maintain that order.
                for i,e in enumerate(feed['entries']):
                    feed['entries'][i]['ord'] = i
                #sort feed entries by episode
                ordkey = lambda e: self._parse_summary(e['summary'])['episode'] * 100 + e['ord']
                entries += sorted(feed['entries'], key=ordkey)
                eps = [self._parse_summary(e['summary'])['episode'] for e in entries]
                log.debug('   Found episodes: %s' % [int(s) for s in set(eps)])
            
            added = 0
            for entry in entries:
                if count >= max_concurrent:
                    log.info('Reached maximum concurrent torrents (%d) for this show "%s".' % (max_concurrent, cfg_name))
                    break;

                link = entry['link']
                summary = entry['summary']

                # parse summary details (assuming ezrss keeps this consistent)
                # ex: 'Show Name: Dexter; Episode Title: My Bad; Season: 5; Episode: 1'
                info = self._parse_summary(summary)
                log.debug('Found: %(show_name)s: Season: %(season)s; Episode: %(episode)s; Title: %(title)s' % info)

                season = int(info['season'])
                episode = int(info['episode'])

                # skip if less than start_episode
                e2n = lambda s,e: int(s)*100 + int(e) # eg, s04e06 would be 406
                if (e2n(season, episode) < e2n(show['start_season'], show['start_episode'])):
                    log.debug('Skipping, s%02de%02d is earlier than start_episode' % (season, episode))
                    continue

                # Check and see if we need this episode
                c = self.db.cursor()
                c.execute('SELECT COUNT() FROM shows WHERE cfg_name=? AND season=? AND episode=?', (cfg_name, season, episode))
                if c.fetchone()[0] > 0:
                    # already have this one, or are already downloading it.
                    log.debug('"%(show_name)s-%(season)s-%(episode)s" has already been downloaded or is currently downloading' % info)
                    continue
                
                # Get the show title
                if info.get('title') == 'N/A':
                    info['title'] = ''

                # Many torrents on ezrss don't come with the episode title. Try the tvdb api.
                if not info.get('title'):
                    log.debug('Trying to find episode title for %(show_name)s-%(season)s-%(episode)s' % info)
                    try:
                        tvdb = tvdb_api.Tvdb()
                        tvdb_episode = tvdb[info['show_name']][int(info['season'])][int(info['episode'])]
                        info['title'] = tvdb_episode['episodename']
                        log.debug('Found: %s' % info['title'])
                    except:
                        # If any that fails, we can't get the title, so...
                        pass
                
                # Get torrent file so that we can parse info out of it
                log.debug('Decoding torrent...')
                try:
                    response = urllib2.urlopen(link)
                except urllib2.HTTPError as e:
                    log.debug('Could not download torrent: %s, %s' % (link,e))
                    continue

                try:
                    torrent = bencode.bdecode(response.read())
                except ValueError:
                    log.debug('Could not parse torrent: %s' % link)
                    continue

                filename = torrent['info'].get('name')
                if not filename:
                    files = torrent['info']['files']
                    # get largest file
                    files = sorted(files, key=lambda f: f['length'], reverse=True)
                    filename = files[0]['path']
                
                ext = os.path.splitext(filename)[1][1:]
                if ext in show['exclude_extensions'].split(','):
                    log.debug('Skipping %s, file extension blacklisted' % filename)
                    continue
                    
                # Add the show
                added += 1
                log.info('Adding %(show_name)s-%(season)s-%(episode)s to transmission queue' % info)
                log.debug(link)
                try:
                    trans_info = self.trans_client.add_uri(link)
                except transmissionrpc.transmission.TransmissionError as e:
                    if '"duplicate torrent"' in str(e):
                        log.info('Torrent already exists. Resuming.')
                        # TODO: Find the duplicate torrent
                        binfo = bencode.bencode(torrent['info'])
                        hash = hashlib.sha1(binfo)
                        trans_info = self.trans_client.inf(hash.hexdigest())
                        self.trans_client.start(trans_info.keys()[0])
                    else:
                        raise

                trans_id = trans_info.keys()[0]
                torrent = trans_info.values()[0]
                log.debug(str(trans_info))
                
                # Record in db
                c = self.db.cursor()
                c.execute('INSERT INTO shows (name, season, episode, title, status, url, transid, cfg_name) VALUES (?, ?, ? ,? ,? ,?, ?, ?)',
                        (show['name'], season, episode, info.get('title'), STATUS_INCOMPLETE, link, trans_id, cfg_name))
                self.db.commit()
                count += 1

            if added == 0:
                log.info('No new episodes found for %s' % show['name'])

    def check_progress(self):
        log.debug('Checking progress')

        globals = self.config.items('globals', SHOW_DEFAULTS)

        # Check for removed torrents
        c = self.db.cursor()
        c.execute('SELECT name, season, episode, title, status, url, transid, cfg_name FROM shows WHERE status=? OR status=?', (STATUS_INCOMPLETE, STATUS_SEEDING))
        for row in c:
            show_name, season, episode, title, status, url, transid, cfg_name = row
            show_cfg = self.config.items(cfg_name, globals)
            seed_ratio = float(show_cfg.get('seed_ratio', 1))
            try:
                torrent = self.trans_client.info(transid)[transid]
            except KeyError:
                # Torrent was removed, so remove from our db
                c2 = self.db.cursor()
                c2.execute('DELETE FROM shows WHERE transid=?', (transid,))
                self.db.commit()
                log.info('Torrent removed: %s' % url)
            else:
                download_dir = torrent.fields['downloadDir']
                # otherwise, check the status
                if status == STATUS_INCOMPLETE and torrent.progress == 100:
                    # The largest file is likely the one we want.
                    file = sorted(torrent.files().values(), key=lambda f: f['size'], reverse=True)[0]['name']

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
                    # if we don't need the file around for seeding, moving is faster
                    if torrent.ratio >= seed_ratio:
                        shutil.move(os.path.join(download_dir, file), destination)
                    else:
                        shutil.copy(os.path.join(download_dir, file), destination)
                    
                    # Make sure torrent is seeding
                    self.trans_client.start(transid)
                    c2 = self.db.cursor()
                    c2.execute('UPDATE shows SET status=? WHERE transid=?', (STATUS_SEEDING, transid))
                    self.db.commit()
                    status = STATUS_SEEDING
                    log.info('Saved %s-s%02de%02d to %s' % (show_name, season, episode, destination))

                elif status == STATUS_INCOMPLETE and torrent.status=='stopped':
                    log.info('Resuming %s-s%02de%02d' % (show_name, season, episode))
                    self.trans_client.start(transid)

                if status == STATUS_SEEDING and torrent.ratio >= seed_ratio:
                    c2 = self.db.cursor()
                    c2.execute('UPDATE shows SET status=? WHERE transid=?', (STATUS_COMPLETE, transid))
                    self.db.commit()
                    log.info('Stopping torrent %s-s%02de%02d' % (show_name, season, episode))
                    # stop the torrent
                    self.trans_client.stop(transid)

                    # Clean up remaining files
                    log.debug('Cleaning up...')
                    files = []
                    dirs = []
                    for num, file in torrent.files().iteritems():
                        file = file['name']
                        # The file path should be relative, but we'll do this to be safe.
                        if file.startswith('/'):
                            continue 
                        file = file.replace(download_dir, '') #also for safetey

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
                                #if it's alread deleted, we don't care
                                raise

                    for dir in dirs:
                        log.debug('Deleted %s' % dir)
                        if dir: #safety
                            shutil.rmtree(os.path.join(download_dir, dir))
                    
                    self.trans_client.remove(transid)

    def reset_show(self, show):
        # load coonfig
        if not show in self.config.sections():
            raise UserError("Show does not exist: %s" % show)
        c = self.db.cursor()
        c.execute('DELETE FROM shows WHERE cfg_name=?', (show,))
        self.db.commit()
        log.info('Successfully deleted history for show "%s"' % show)

    def _parse_summary(self, summary):
        summary_data = dict([i.split(': ') for i in summary.split('; ')])
        info = {
            'show_name': summary_data.get('Show Name'),
            'season': int(summary_data.get('Season')),
            'episode': int(summary_data.get('Episode')),
            'title': summary_data.get('Episode Title'),
        }
        return info


if __name__ == "__main__":
    #parse options
    option_list = [
        make_option('--reset-show', action='store', type='string', help="Delete a show's download history", metavar="NAME"),
        make_option('--check-progress', action='store_const', const='check_progress', dest='action', help="Check progress of currently downloading episodes."),
        make_option('--find-new', action='store_const', const='find_new', dest='action', help="Check RSS feed for new episodes."),
    ]
    parser = OptionParser(option_list=option_list)
    options, args = parser.parse_args()

    try:
        if options.reset_show:
            fetcher = TvFetch()
            fetcher.reset_show(options.reset_show)
            sys.exit(0)

        if options.action == 'check_progress':
            fetcher = TvFetch()
            fetcher.check_progress()
            sys.exit(0)

        if options.action == 'find_new':
            fetcher = TvFetch()
            fetcher.check_new()
            sys.exit(0)

        parser.print_help()
        sys.exit(1)

    except UserError as e:
        log.error(e)
    except Exception as e:
        log.exception(e)
    finally:
        sys.exit(1)
