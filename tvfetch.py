import ConfigParser
import feedparser
import os, shutil
import sqlite3
import transmissionrpc
import logging
import logging.handlers

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
    'start_episode': 1
}
LOG_FILENAME='/var/log/%s.log' % NAME
LOG_LEVELS = {
    'debug': logging.DEBUG,
    'info': logging.INFO,
    'warning': logging.WARNING,
    'error': logging.ERROR,
    'critical': logging.CRITICAL
}
CONFIG_FILE = '%s.conf' % NAME

feed_url = 'http://ezrss.it/search/?mode=rss&show_name=%(show_name)s&quality=%(quality)s'

# setup logging

log = logging.getLogger('%s_log' % NAME)
log.setLevel(logging.INFO)

# rotated log
handler = logging.handlers.RotatingFileHandler(LOG_FILENAME, maxBytes=1024, backupCount=5)
formatter = logging.Formatter("%(asctime)s: %(levelname)s: %(message)s")
handler.retFormatter(formatter)
log.addHandler(handler)

# console
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(formatter)
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
            data = self.config.items(section)
        except ConfigParser.NoSectionError:
            data = {}
        result.update(d)
        return result

    def sections(self):
        return self.sections()

config = Config()
logging.debug('Loaded config')

log_level = LOG_LEVELS.get(config.get('globals', 'log_level', 'info'))
log.setLevel(log_level)

logging.info('Running...')

# setup database
db_exists = os.path.exists(DB)
db = sqlite3.connect(DB)
if not db_exists:
    c = db.cursor()
    c.execute('create table shows(name text, season integer, episode integer, title text, status text, url text, transid integer, cfg_name text')
    logging.debug('Created initial database')

#get transmission client
trans_cfg = config.items('transmission', {'host':'localhost','port':'9091'})
trans_client = transmissionrpc.Client(**trans_cfg)

#find our shows
shows = [s for s in config.sections() if s not in ('transmission', 'globals')]
globals = config.items('globals', SHOW_DEFAULTS)

for s in shows:
    show = config.items(s, globals)
    show['name'] = show.get('name', s)
    logging.info('Looking up %s' % show['name'])
    
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
        log.debug('%(show_name)s: Season: %(season)s; Episode: %(episode)s; Title: %(title)s' % info)

        season = info.get('season')
        episode = info.get('episode')

        # skip if less than start_episode
        if (season < show['start_season']) or (episode < show['start_episode']):
            continue

        # Check and see if we need this episode
        c = db.cursor()
        c.execute('SELECT status FROM shows WHERE name=? AND season=? AND episode=?', (show['name'], season, episode))
        if len(c) > 0:
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
                s = int(info.get('season'))
                e = int(info.get('episode'))
                tvdb_episode = tvdb[info['show_name']][s][e]
                info['title'] = tvdb_episode['episodename']
                log.debug('Found: %s' % info['title'])
            except:
                # If any that fails, we can't get the title, so...
                pass
        

        # Add the show
        log.info('Adding %(show_name)s-%(season)s-%(episode)s to transmission queue')
        log.debug(link)
        trans_info = trans_client.add_url(link)
        trans_id = trans_info.keys()[0]
        torrent = trans_info.values()[0]
        log.debug(str(trans_info))
        
        # Record in db
        c = db.cursor()
        c.execute('INSERT INTO shows (name, season, episode, title, status, url, transid, cfg_name) VALUES (?, ?, ? ,? ,? ,?, ?, ?)',
                (show['name'], season, episode, info.get('title'), 'I', link, trans_id, s))



log.debug('Checking for completed shows')

# Check for completed shows
c = db.cursor()
c.execute('SELECT name, season, episode, title, status, url, transid, cfg_name FROM shows WHERE status=? OR status=?', (STATUS_INCOMPLETE, STATUS_SEEDING))
for row in c:
    show_name, season, episode, title, status, url, transid, cfg_name = row
    show_cfg = config.items(cfg_name, globals)
    seed_ratio = show_cfg.get('seed_ratio', 1)
    try:
        torrent = trans_client.info(transid)[transid]
    except KeyError:
        # Torrent was removed, so remove from our db
        c2 = db.cursor()
        c2.execute('DELETE FROM shows WHERE transid=?', transid)
    else:
        # otherwise, check the status
        if torrent.status == STATUS_INCOMPLETE and torrent.progress == 100:
            # The largest file is likely the one we want.
            file = sorted(torrent.files().values(), key=lambda f: f['size'])[0]['name']
            # Move the file to its final destination
            download_dir = torrent.fields['downloadDir']
            destination = show_cfg['destination'] % {
                'show_name': show_name,
                'season': season,
                'episode': episode,
                'title': title,
            }
            # if we don't need the file around for seeding, moving is faster
            if torrent.ratio >= seed_ratio:
                shutil.move(os.path.join(download_dir, file), destination)
            else:
                shutil.copy(os.path.join(download_dir, file), destination)
            log.info('Saved %s-%s-%s to %s' % (show_name, season, episode, destination))

        if torrent.ratio >= seed_ratio:
            log.info('Stopping torrent %s-%s-%s' % show_name, season, episode)
            # stop the torrent
            trans_client.stop(transid)

            # Clean up remaining files
            log.debug('Cleaning up...')
            files = []
            dirs = []
            for file in torrent.files():
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
