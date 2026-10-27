#!/usr/bin/env python3
"""Мост reconcept.ru <-> Telegram. Посетитель = тема в группе; ответ в теме уходит на сайт. Только stdlib."""
import json, logging, os, sqlite3, threading, time, urllib.error, urllib.request

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
log = logging.getLogger('chat')
TOKEN, CHAT, KEY = os.environ['TG_TOKEN'], int(os.environ['TG_CHAT']), os.environ['CHAT_SECRET']
SITE = os.environ.get('SITE', 'https://reconcept.ru').rstrip('/')
TG = os.environ.get('TG_API', 'https://api.telegram.org').rstrip('/') + f'/bot{TOKEN}/'
MEDIA = ('photo', 'document', 'sticker', 'voice', 'video', 'video_note', 'audio', 'animation')

db = sqlite3.connect(os.path.join(os.environ.get('STATE_DIRECTORY', os.path.dirname(os.path.abspath(__file__))), 'state.db'),
                     check_same_thread=False, isolation_level=None)
db.executescript('CREATE TABLE IF NOT EXISTS topic(sid TEXT PRIMARY KEY, thread INTEGER UNIQUE, page TEXT);'
                 'CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);')
lock = threading.Lock()


def sql(query, *args):
    with lock:
        return db.execute(query, args).fetchone()


def kv(k, v=None):
    if v is None:
        r = sql('SELECT v FROM kv WHERE k=?', k)
        return int(r[0]) if r else 0
    sql('INSERT OR REPLACE INTO kv VALUES(?,?)', k, str(v))


def call(url, data=None, headers=None, timeout=40):
    req = urllib.request.Request(url, None if data is None else json.dumps(data).encode(),
                                 {'Content-Type': 'application/json', 'User-Agent': 'reconcept-chat', **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


class TgError(Exception):
    pass


def tg(method, _t=40, **p):
    while True:
        try:
            r = call(TG + method, p, timeout=_t)
        except urllib.error.HTTPError as e:
            try:
                r = json.load(e)
            except ValueError:
                raise TgError(f'HTTP {e.code}')
        if r.get('ok'):
            return r['result']
        wait = (r.get('parameters') or {}).get('retry_after')
        if not wait:
            raise TgError(r.get('description', '?'))
        time.sleep(wait)


def site(path, data=None):
    return call(f'{SITE}/api/chat/bridge/{path}', data, {'Authorization': f'Bearer {KEY}'})


def topic(sid, meta):
    r = sql('SELECT thread, page FROM topic WHERE sid=?', sid)
    if r:
        return r
    page = meta.get('page') or '/'
    t = tg('createForumTopic', chat_id=CHAT, name=f'{page} · {sid[:4]}'[:120])['message_thread_id']
    info = [f'Страница: {SITE}{page}', meta.get('ref') and f"Откуда: {meta['ref']}", meta.get('ua') and f"Браузер: {meta['ua']}"]
    tg('sendMessage', chat_id=CHAT, message_thread_id=t, text='\n'.join(x for x in info if x),
       link_preview_options={'is_disabled': True})
    sql('INSERT OR REPLACE INTO topic VALUES(?,?,?)', sid, t, page)
    return t, page


def to_tg(m, retry=True):
    meta = json.loads(m.get('meta') or '{}')
    t, last = topic(m['sid'], meta)
    text, page = m['t'], meta.get('page')
    if page and page != last:
        text = f'[{page}]\n{text}'
    try:
        tg('sendMessage', chat_id=CHAT, message_thread_id=t, text=text)
    except TgError as e:
        if retry and 'thread' in str(e).lower():  # тему удалили — заводим заново
            sql('DELETE FROM topic WHERE sid=?', m['sid'])
            return to_tg(m, False)
        raise
    if page and page != last:
        sql('UPDATE topic SET page=? WHERE sid=?', page, m['sid'])


def site_loop():
    while True:
        try:
            for m in site(f"pull?after={kv('after')}"):
                to_tg(m)
                kv('after', m['id'])
        except Exception as e:
            log.warning('site: %s', e)
            time.sleep(5)


def tg_loop():
    while True:
        try:
            for u in tg('getUpdates', _t=70, offset=kv('off'), timeout=50, allowed_updates=['message']):
                msg = u.get('message') or {}
                th = msg.get('message_thread_id')
                if msg.get('chat', {}).get('id') == CHAT and th and not msg.get('from', {}).get('is_bot'):
                    r = sql('SELECT sid FROM topic WHERE thread=?', th)
                    if r and msg.get('text'):
                        try:
                            site('push', {'sid': r[0], 'text': msg['text']})
                        except urllib.error.HTTPError as e:
                            if e.code != 404:
                                raise
                            tg('sendMessage', chat_id=CHAT, message_thread_id=th, text='⚠️ Диалог на сайте удалён (старше 30 дней).')
                    elif r and any(k in msg for k in MEDIA):
                        tg('sendMessage', chat_id=CHAT, message_thread_id=th, text='⚠️ На сайт уходит только текст.')
                kv('off', u['update_id'] + 1)
        except Exception as e:
            log.warning('tg: %s', e)
            time.sleep(5)


if __name__ == '__main__':
    tg('deleteWebhook')
    threading.Thread(target=site_loop, daemon=True).start()
    log.info('started')
    tg_loop()
