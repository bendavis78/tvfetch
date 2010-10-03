import ConfigParser
import feedparser
import os, shutil, errno
import sqlite3
import transmissionrpc
import logging
import urllib2
from logging import handlers

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
    'exclude_extensions': 'rar'

}
LOG_FILENAME='/var/log/%s/%s.log' % (NAME, NAME)
LOG_FORMAT='%(asctime)s: %(levelname)s: %(message)s'
LOG_LEVELS = {
    'debug': logging.DEBUG,
    'info': logging.INFO,
    'warning': logging.WARNING,
    'error': logging.ERROR,
    'critical': logging.CRITICAL
}
CONFIG_FILE = '%s.conf' % NAME


class Decoder(object):
    """ Torrent Decoder """
    def __init__(self, data): self.data, self.ptr = data, 0
    def _cur(self): return self.data[self.ptr]
    def _get(self, x):
        self.ptr += x
        return self.data[self.ptr-x:self.ptr]
    def _get_int_until(self, c):
        num = int(self._get(self.data.index(c, self.ptr)-self.ptr))
        self._get(1) # kill extra char
        return num
    def _get_str(self): return self._get(self._get_int_until(":"))
    def _get_int(self): return self._get_int_until("e")
    def decode(self):
        i = self._get(1)
        if i == "d":
            r = {}
            while self._cur() != "e":
                key = self._get_str()
                val = self.decode()
                r[key] = val
            self._get(1)
        elif i == "l":
            r = []
            while self._cur() != "e": r.append(self.decode())
            self._get(1)
        elif i == "i": r = self._get_int()
        elif i.isdigit():
            self._get(-1) # reeeeewind
            r = self._get_str()
        return r

feed_url = 'http://ezrss.it/search/?mode=rss&show_name=%(show_name)s&quality=%(quality)s'

# setup logging

log = logging.getLogger('%s_log' % NAME)
log.setLevel(logging.INFO)

# rotated log
handler = handlers.RotatingFileHandler(LOG_FILENAME, maxBytes=1024, backupCount=5)
handler.setFormatter(logging.Formatter(LOG_FORMAT))
log.addHandler(handler)

# console output
ch = logging.StreamHandler()
ch.setFormatter(logging.Formatter(LOG_FORMAT))
log.addHandler(ch)

# load config

class Config(object):
    def __init__(self):
        self.config = ConfigParser.ConfigParser()
        self.config.read(CONFIG_FILE)
        self.globals = self.config.items('globals', 1)
    
    def get(self, section, key, default=None):
        try:
            return self.config.get(section, key)
        except ConfigParser.NoOptionError:
            return default
    
    def items(self, section, defaults={}):
        result = defaults.copy()
        try:
            data = self.config.items(section, 1)
        except ConfigParser.NoSectionError:
            data = {}
        result.update(data)
        return result

    def sections(self):
        return self.config.sections()

config = Config()
log.debug('Loaded config')

log_level = LOG_LEVELS.get(config.get('globals', 'log_level', 'info'))
log.setLevel(log_level)

log.info('Running...')

# setup database
db_exists = os.path.exists(DB)
db = sqlite3.connect(DB)
if not db_exists:
    c = db.cursor()
    c.execute('create table shows(name text, season integer, episode integer, title text, status text, url text, transid integer, cfg_name text)')
    log.debug('Created initial database')

#get transmission client
trans_cfg = config.items('transmission', {'host':'localhost','port':'9091'})
trans_cfg['address'] = trans_cfg.pop('host')
trans_client = transmissionrpc.Client(**trans_cfg)

#find our shows
shows = [s for s in config.sections() if s not in ('transmission', 'globals')]
globals = config.items('globals', SHOW_DEFAULTS)

for s in shows:
    show = config.items(s, globals)
    show['name'] = show.get('name', s)
    log.debug('Looking up %s' % show['name'])
    
    #load rss feed:
    feed = feedparser.parse(feed_url % {'show_name': show['name'], 'quality': show.get('quality')})
    log.debug('found %d entries for %s' % (len(feed['entries']), show['name']))
    for entry in feed['entries']:
        link = entry['link']
        summary = entry['summary']
        title = entry['title']

        # parse summary details (assuming ezrss keeps this consistent)
        # ex: 'Show Name: Dexter; Episode Title: My Bad; Season: 5; Episode: 1'
        summary_data = dict([i.split(': ') for i in summary.split('; ')])
        info = {
            'show_name': summary_data.get('Show Name'),
            'season': summary_data.get('Season'),
            'episode': summary_data.get('Episode'),
            'title': summary_data.get('Episode Title'),
        }
        log.debug('Found: %(show_name)s: Season: %(season)s; Episode: %(episode)s; Title: %(title)s' % info)

        season = int(info['season'])
        episode = int(info['episode'])

        # skip if less than start_episode
        
        e2n = lambda s,e: int(s)*100 + int(e) # eg, s04e06 would be 406
        if (e2n(season, episode) < e2n(show['start_season'], show['start_episode'])):
            log.debug('Skipping, s%02de%02d is earlier than start_episode' % (season, episode))
            continue

        # Check and see if we need this episode
        c = db.cursor()
        c.execute('SELECT COUNT() FROM shows WHERE cfg_name=? AND season=? AND episode=?', (s, season, episode))
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
                import tvdb_api
                tvdb = tvdb_api.Tvdb()
                tvdb_episode = tvdb[info['show_name']][int(info['season'])][int(info['episode'])]
                info['title'] = tvdb_episode['episodename']
                log.debug('Found: %s' % info['title'])
            except:
                # If any that fails, we can't get the title, so...
                pass
        
        # Get torrent file so that we can parse info out of it
        log.debug('Decoding torrent...')
        response = urllib2.urlopen(link)
        decoder = Decoder(response.read())
        torrent = decoder.decode()
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
        log.info('Adding %(show_name)s-%(season)s-%(episode)s to transmission queue' % info)
        log.debug(link)
        try:
            trans_info = trans_client.add_uri(link)
        except transmissionrpc.transmission.TransmissionError as e:
            if '"duplicate torrent"' in str(e):
                log.info('Torrent alredy added. Resuming.')
                # TODO: Find the duplicate torrent
                import ipdb; ipdb.set_trace()
        trans_id = trans_info.keys()[0]
        torrent = trans_info.values()[0]
        log.debug(str(trans_info))
        
        # Record in db
        c = db.cursor()
        c.execute('INSERT INTO shows (name, season, episode, title, status, url, transid, cfg_name) VALUES (?, ?, ? ,? ,? ,?, ?, ?)',
                (show['name'], season, episode, info.get('title'), 'I', link, trans_id, s))
        db.commit()

    else:
        log.info('No new episodes found for %s' % show['name'])

log.debug('Checking for completed shows')

# Check for completed shows
c = db.cursor()
c.execute('SELECT name, season, episode, title, status, url, transid, cfg_name FROM shows WHERE status=? OR status=?', (STATUS_INCOMPLETE, STATUS_SEEDING))
for row in c:
    show_name, season, episode, title, status, url, transid, cfg_name = row
    show_cfg = config.items(cfg_name, globals)
    seed_ratio = float(show_cfg.get('seed_ratio', 1))
    try:
        torrent = trans_client.info(transid)[transid]
    except KeyError:
        # Torrent was removed, so remove from our db
        c2 = db.cursor()
        c2.execute('DELETE FROM shows WHERE transid=?', (transid,))
        db.commit()
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

            c2 = db.cursor()
            c2.execute('UPDATE shows SET status=? WHERE transid=?', (STATUS_SEEDING, transid))
            db.commit()
            status = STATUS_SEEDING
            log.info('Saved %s-%s-%s to %s' % (show_name, season, episode, destination))
        
        if status == STATUS_SEEDING and torrent.ratio >= seed_ratio:
            c2 = db.cursor()
            c2.execute('UPDATE shows SET status=? WHERE transid=?', (STATUS_COMPLETE, transid))
            db.commit()
            log.info('Stopping torrent %s-%d-%d' % (show_name, season, episode))
            # stop the torrent
            trans_client.stop(transid)

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
                os.remove(os.path.join(download_dir, file))

            for dir in dirs:
                log.debug('Deleted %s' % dir)
                if dir: #safety
                    shutil.rmtree(os.path.join(download_dir, dir))
            
            trans_client.remove(transid)
