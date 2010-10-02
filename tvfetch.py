import feedparser
import os, shutil
import sqlite3
import transmissionrpc

# Config (TODO: use a config file)
SHOWS = [
    {'name':'Dexter', 'start_season': 5, 'start_episode': 1},
    #{'name':'Weeds', 'start_season': 6, 'start_episode': 7}
]

QUALITY = 'HDTV'
DESTINATION = '/media/vault/Video/TV Shows/%(show_name)s/Season %(season)d/Episode %(episode)d - %(title)s'

TRANSMISSION_HOST = 'localhost'
TRANSMISSION_PORT = '9091'
TRANSMISSION_USERNAME = 'ben'
TRANSMISSION_PASSWORD = 'BodsOajcom5'

SEED_RATIO = 1 # Be kind. Share and share alike

#constants
DB = '/var/tvfetch/db'
STATUS_COMPLETE='C'
STATUS_INCOMPLETE='I'

feed_url = 'http://ezrss.it/search/?mode=rss&show_name=%(show_name)s&quality=%(quality)s'

#setup database
db_exists = os.path.exists(DB)
db = sqlite3.connect(DB)
if not db_exists:
    c = db.cursor()
    c.execute('create table shows(name text, season integer, episode integer, title text, status text, url text, transid integer')

#get transmission client
trans_client = transmissionrpc.Client(TRANSMISSION_HOST, TRANSMISSION_PORT, TRANSMISSION_USERNAME, TRANSMISSION_PASSWORD)

#find our shows
for show in SHOWS:
    #load rss feed:
    data = {
        'show_name': show['name'],
        'quality': QUALITY,
    }
    feed = feedparser.parse(feed_url % data)
    for entry in feed.get('entries'):
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

        #Check and see if we need this episode
        c = db.cursor()
        season = info.get('season')
        episode = info.get('episode')
        c.execute('SELECT status FROM shows WHERE name=? AND season=? AND episode=?', (show['name'], season, episode))
        if len(c) > 0:
            # already have this one, or are already downloading it.
            continue
        
        # Get the show title
        if info.get('title') == 'N/A':
            info['title'] = ''

        # Many torrents on ezrss don't come with the episode title. Try the tvdb api.
        if not info.get('title'):
            try:
                import tvdb_api
                tvdb = tvdb_api.Tvdb()
                s = int(info.get('season'))
                e = int(info.get('episode'))
                tvdb_episode = tvdb[info['show_name']][s][e]
                info['title'] = tvdb_episode['episodename']
            except:
                # If any that fails, we can't get the title, so...
                pass
        

        # Add the show
        trans_info = trans_client.add_url(link)
        trans_id = trans_info.keys()[0]
        torrent = trans_info.values()[0]
        
        # Record in db
        c = db.cursor()
        c.execute('INSERT INTO shows (name, season, episode, title, status, url, transid) VALUES (?, ?, ? ,? ,? ,?, ?)',
                (show['name'], season, episode, info.get('title'), 'I', link, trans_id))


# Check for completed shows
c = db.cursor()
c.execute('SELECT name, season, episode, title, status, url, transid FROM shows WHERE status=?', 'I')
for row in c:
    show_name, season, episode, title, status, url, transid = row
    try:
        torrent = trans_client.info(transid)[transid]
    except KeyError:
        # Torrent was removed, so remove from our db
        c2 = db.cursor()
        c2.execute('DELETE FROM shows WHERE transid=?', transid)
    else:
        # otherwise, check the status
        if torrent.progress == 100:
            # Pause the torrent
            # The largest file is likely the one we want.
            file = sorted(torrent.files().values(), key=lambda f: f['size'])[0]['name']
            # Move the file to its final destination
            download_dir = torrent.fields['downloadDir']
            destination = DESTINATION % {
                'show_name': show_name,
                'season': season,
                'episode': episode,
                'title': title,
            }
            shutil.move(os.path.join(downloar_dir, file), destination)

            # Clean up remaining files
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
                os.remove(os.path.join(download_dir, file))

            for dir in dirs:
                if dir: #safety
                    shutil.rmtree(os.path.join(download_dir, dir))

