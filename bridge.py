#!/usr/bin/env python3
"""Мост reconcept.ru <-> Telegram. Посетитель = тема в группе; ответ в теме уходит на сайт. Только stdlib."""
import json, logging, re, os, sqlite3, threading, time, urllib.error, urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
log = logging.getLogger('chat')
TOKEN, CHAT, KEY = os.environ['TG_TOKEN'], int(os.environ['TG_CHAT']), os.environ['CHAT_SECRET']
SITE = os.environ.get('SITE', 'https://reconcept.ru').rstrip('/')
TG = os.environ.get('TG_API', 'https://api.telegram.org').rstrip('/') + f'/bot{TOKEN}/'
AI_KEY, AI_MODEL = os.environ.get('ANTHROPIC_API_KEY', ''), os.environ.get('AI_MODEL', 'claude-haiku-5-5')
KNOW = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'knowledge.md')
TZ = ZoneInfo('Europe/Belgrade')
PROMPT = ('Ты — Робот-секретарь Сергея Веснина (веб-студия Reconcept) в чате на сайте reconcept.ru. Сергей сейчас не на связи; '
          'посетителю уже сказали, что ты его помощник, — не повторяй это и не упоминай Сергея без нужды. '
          'Цель: коротко ответить на вопрос и получить контакт, чтобы Сергей связался с посетителем. '
          'Пиши по-русски, 1–3 предложения, дружелюбно и просто, без markdown, списков и приветствий. '
          'Опирайся только на факты ниже; цены называй со словом «от». Не обещай точные сроки, скидки и результаты, не придумывай факты. '
          'Чего нет в фактах — скажи, что это обсудит Сергей. '
          'Ничего не требуй от посетителя: никаких текстов, фото, ТЗ, подробностей. Уточняющий вопрос — только если без него нельзя ответить. '
          'Ответив, предложи оставить удобный контакт — Telegram, MAX, телефон или почту, — чтобы Сергей связался. '
          'Если контакт уже оставлен — поблагодари и скажи, что Сергей свяжется; больше контакт не проси. '
          'Если посетитель не хочет оставлять контакт — не настаивай: Сергей ответит здесь, в чате. '
          'Не выдавай себя за Сергея: если спросят, человек ли ты, скажи, что ты робот-секретарь. '
          'На темы, не связанные с услугами студии, не отвечай.\n\n')
HELLO = 'Сергей не может ответить прямо сейчас. Я его помощник — постараюсь помочь, а Сергей подключится позже.'
timers, lead = {}, set()  # lead — диалоги, которые сейчас ведёт ИИ
CONTACT = re.compile(r'(?:\+?[78][\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2})|[\w.+-]+@[\w-]+\.[\w.-]+|(?<![\w@])@[A-Za-z]\w{3,31}|t\.me/\S+|max\.ru/\S+', re.I)
MEDIA = ('photo', 'document', 'sticker', 'voice', 'video', 'video_note', 'audio', 'animation')

db = sqlite3.connect(os.path.join(os.environ.get('STATE_DIRECTORY', os.path.dirname(os.path.abspath(__file__))), 'state.db'),
                     check_same_thread=False, isolation_level=None)
db.executescript('CREATE TABLE IF NOT EXISTS topic(sid TEXT PRIMARY KEY, thread INTEGER UNIQUE, page TEXT);'
                 'CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);')
for c in ('ai INTEGER DEFAULT 1', 'btn INTEGER'):
    try:
        db.execute('ALTER TABLE topic ADD COLUMN ' + c)
    except sqlite3.OperationalError:
        pass
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
    sql('INSERT OR REPLACE INTO topic(sid, thread, page) VALUES(?,?,?)', sid, t, page)
    return t, page


def kb(on):
    return {'inline_keyboard': [[{'text': ('✅ ' if on else '') + 'ИИ вкл', 'callback_data': 'ai:1'},
                                 {'text': ('' if on else '✅ ') + 'ИИ выкл', 'callback_data': 'ai:0'}]]}


def button(sid, th, mid):
    """Кнопки ИИ — только под последним сообщением посетителя."""
    ai, old = sql('SELECT ai, btn FROM topic WHERE sid=?', sid)
    if old:
        try:
            tg('editMessageReplyMarkup', chat_id=CHAT, message_id=old, reply_markup={'inline_keyboard': []})
        except TgError:
            pass
    tg('editMessageReplyMarkup', chat_id=CHAT, message_id=mid, reply_markup=kb(ai))
    sql('UPDATE topic SET btn=? WHERE sid=?', mid, sid)


def typing(sid, d, ms):
    """Индикатор «…пишет» на сайте: d=1 Сергей, d=2 ИИ; ms=0 — убрать."""
    try:
        site('typing', {'sid': sid, 'd': d, 'ms': ms})
    except Exception as e:
        log.warning('typing: %s', e)


def set_ai(sid, on):
    sql('UPDATE topic SET ai=? WHERE sid=?', int(on), sid)
    lead.discard(sid)
    typing(sid, 1, 0 if on else 180000)  # «ИИ выкл» = Сергей отвечает
    schedule(sid) if on else cancel(sid)


def to_tg(m, retry=True):
    meta = json.loads(m.get('meta') or '{}')
    t, last = topic(m['sid'], meta)
    text, page = m['t'], meta.get('page')
    if page and page != last:
        text = f'[{page}]\n{text}'
    try:
        mid = tg('sendMessage', chat_id=CHAT, message_thread_id=t, text=text)['message_id']
    except TgError as e:
        if retry and 'thread' in str(e).lower():  # тему удалили — заводим заново
            sql('DELETE FROM topic WHERE sid=?', m['sid'])
            return to_tg(m, False)
        raise
    if page and page != last:
        sql('UPDATE topic SET page=? WHERE sid=?', page, m['sid'])
    found = CONTACT.findall(m['t'])
    if found:
        try:
            c = tg('sendMessage', chat_id=CHAT, message_thread_id=t, text='📇 Контакт: ' + ', '.join(dict.fromkeys(found)))
            tg('pinChatMessage', chat_id=CHAT, message_id=c['message_id'], disable_notification=True)
        except TgError as e:
            log.warning('pin: %s', e)
    try:
        button(m['sid'], t, mid)
    except TgError as e:
        log.warning('btn: %s', e)


def schedule(sid):
    """ИИ отвечает, если Сергей молчит минуту; с 22:00 до 6:00 (Белград) и в диалоге, который ИИ уже ведёт, — сразу."""
    r = sql('SELECT ai FROM topic WHERE sid=?', sid)
    if not AI_KEY or not r or not r[0]:
        return
    h = datetime.now(TZ).hour
    if sid not in lead:  # после перезапуска: ведёт ИИ, если из ответов последний — его
        try:
            if next((m['d'] for m in reversed(site(f'history?sid={sid}')) if m['d']), 0) == 2:
                lead.add(sid)
        except Exception as e:
            log.warning('lead: %s', e)
    cancel(sid)
    t = timers[sid] = threading.Timer(1 if sid in lead else 3 if h >= 22 or h < 6 else 60, ai_reply, (sid,))
    t.daemon = True
    t.start()


def cancel(sid):
    t = timers.pop(sid, None)
    if t:
        t.cancel()


def ai_reply(sid):
    timers.pop(sid, None)
    sent = False
    try:
        hist = site(f'history?sid={sid}')
        if not hist or hist[-1]['d'] != 0 or sum(m['d'] == 2 for m in hist) >= 30:
            return
        lead.add(sid)  # ИИ взялся отвечать: новые сообщения посетителя — без минутной паузы
        th = sql('SELECT thread FROM topic WHERE sid=?', sid)
        if not any(m['d'] == 2 for m in hist):  # ИИ впервые в диалоге — представляется
            site('push', {'sid': sid, 'text': HELLO, 'd': 2})
            if th:
                tg('sendMessage', chat_id=CHAT, message_thread_id=th[0], text=f'🤖 {HELLO}')
        typing(sid, 2, 60000)
        t0 = time.monotonic()
        msgs = []
        for m in hist[-40:]:
            role = 'user' if m['d'] == 0 else 'assistant'
            if msgs and msgs[-1]['role'] == role:
                msgs[-1]['content'] += '\n' + m['t']
            elif msgs or role == 'user':
                msgs.append({'role': role, 'content': m['t']})
        with open(KNOW, encoding='utf-8') as f:
            system = PROMPT + f.read()
        r = call(os.environ.get('AI_API', 'https://api.anthropic.com') + '/v1/messages',
                 {'model': AI_MODEL, 'max_tokens': 500, 'system': system, 'messages': msgs},
                 {'x-api-key': AI_KEY, 'anthropic-version': '2023-06-01'}, timeout=60)
        text = ''.join(b.get('text', '') for b in r.get('content', [])).strip()
        time.sleep(max(0, min(1.5 + len(text) / 60, 6) - (time.monotonic() - t0)))  # «печатает» не мгновенно
        if not text or sid in timers or site(f'history?sid={sid}')[-1]['d'] != 0:  # пока думал, ответил Сергей или пришло новое
            return
        site('push', {'sid': sid, 'text': text, 'd': 2})
        sent = True
        if th:
            tg('sendMessage', chat_id=CHAT, message_thread_id=th[0], text=f'🤖 {text}')
    except Exception as e:
        log.warning('ai: %s', e)
    finally:
        if not sent and sid not in timers:
            typing(sid, 2, 0)


def site_loop():
    while True:
        try:
            for m in site(f"pull?after={kv('after')}"):
                to_tg(m)
                kv('after', m['id'])
                schedule(m['sid'])
        except Exception as e:
            log.warning('site: %s', e)
            time.sleep(5)


def tg_loop():
    while True:
        try:
            for u in tg('getUpdates', _t=70, offset=kv('off'), timeout=50, allowed_updates=['message', 'callback_query']):
                q = u.get('callback_query')
                if q:
                    qm = q.get('message') or {}
                    r = qm.get('chat', {}).get('id') == CHAT and sql('SELECT sid FROM topic WHERE thread=?', qm.get('message_thread_id'))
                    if r and q.get('data') in ('ai:0', 'ai:1'):
                        on = q['data'] == 'ai:1'
                        set_ai(r[0], on)
                        tg('answerCallbackQuery', callback_query_id=q['id'], text='ИИ ' + ('включён' if on else 'выключен'))
                        try:
                            tg('editMessageReplyMarkup', chat_id=CHAT, message_id=qm['message_id'], reply_markup=kb(on))
                        except TgError:
                            pass  # «message is not modified»
                    else:
                        tg('answerCallbackQuery', callback_query_id=q['id'])
                msg = u.get('message') or {}
                th = msg.get('message_thread_id')
                if msg.get('chat', {}).get('id') == CHAT and th and not msg.get('from', {}).get('is_bot'):
                    r = sql('SELECT sid FROM topic WHERE thread=?', th)
                    cmd = (msg.get('text') or '').strip().lower()
                    if r and cmd in ('/ai off', '/ai on'):
                        set_ai(r[0], cmd == '/ai on')
                        tg('sendMessage', chat_id=CHAT, message_thread_id=th, text='ИИ в этом диалоге ' + ('включён' if cmd == '/ai on' else 'выключен'))
                    elif r and msg.get('text'):
                        cancel(r[0])
                        lead.discard(r[0])
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
