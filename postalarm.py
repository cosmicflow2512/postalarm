"""postalarm: X (über RSSHub) und Discord (über DiscordChatExporter) -> Pushover.

Nur Python-Standardbibliothek. Eine Datenbank, eine Zustellwarteschlange, je Quelle ein Thread.
"""
import datetime as dt
import fcntl
import html
import json
import logging
import os
from pathlib import Path
import re
import signal
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger('postalarm')
STOP = threading.Event()
CRASHED = threading.Event()
DELIVER_LOCK = threading.Lock()

PUSHOVER_URL = 'https://api.pushover.net/1/messages.json'
DISCORD_EPOCH_MS = 1420070400000
# Pushover-Limits laut https://pushover.net/api
LIMIT_MESSAGE, LIMIT_TITLE, LIMIT_URL, LIMIT_URL_TITLE = 1024, 250, 512, 100
MODES = ('emergency', 'burst', 'high', 'normal')
# DiscordChatExporter-Formate (ExportFormat.cs) -> (CLI-Wert, Dateiendung)
ARCHIVE_FORMATS = {'htmldark': ('HtmlDark', 'html'), 'htmllight': ('HtmlLight', 'html'),
                   'plaintext': ('PlainText', 'txt'), 'json': ('Json', 'json'), 'csv': ('Csv', 'csv')}
# DCE meldet leere Zeiträume als Warnung (Exit 0) und legt trotzdem eine leere Datei an
DCE_EMPTY_MARKER = 'does not contain any messages'
X_KEEP_SEEN = 500
X_ACCOUNT_RE = re.compile(r'^[A-Za-z0-9_]{1,15}$')
# Nur diese Variablen bekommt DiscordChatExporter; Pushover- und X-Zugangsdaten bleiben draußen.
DCE_ENV = ('PATH', 'HOME', 'TZ', 'LANG', 'LC_ALL', 'DISCORD_TOKEN',
           'DOTNET_SYSTEM_GLOBALIZATION_INVARIANT', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
           'HTTPS_PROXY', 'HTTP_PROXY', 'NO_PROXY', 'https_proxy', 'http_proxy', 'no_proxy')


class ConfigError(Exception):
    """Fehler, deren Text keine Geheimnisse enthält und ausgegeben werden darf."""


class PushoverError(Exception):
    def __init__(self, kind, detail=''):
        super().__init__(f'{kind}: {detail}' if detail else kind)
        self.kind = kind  # 'config' | 'quota' | 'message' | 'temporary'
        self.detail = detail


# ================================================================ Konfiguration

def env(*names, default=''):
    """Erster gesetzter Wert; weitere Namen sind Aliase aus tweetalarm/discord-author-watcher."""
    for name in names:
        value = os.getenv(name, '').strip()
        if value:
            return value
    return default


def integer(names, default, minimum, maximum):
    names = (names,) if isinstance(names, str) else names
    raw = env(*names, default=str(default))
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f'{names[0]} muss eine ganze Zahl sein') from None
    if not minimum <= value <= maximum:
        raise ConfigError(f'{names[0]} muss zwischen {minimum} und {maximum} liegen')
    return value


def boolean(names, default=False):
    names = (names,) if isinstance(names, str) else names
    raw = env(*names).lower()
    if not raw:
        return default
    if raw in ('1', 'true', 'yes', 'on'):
        return True
    if raw in ('0', 'false', 'no', 'off'):
        return False
    raise ConfigError(f'{names[0]} muss true oder false sein')


def choice(names, default, options):
    names = (names,) if isinstance(names, str) else names
    value = env(*names, default=default).lower()
    if value not in options:
        raise ConfigError(f'{names[0]} muss einer von {", ".join(options)} sein')
    return value


def ids(name, required=False):
    values = {v.strip() for v in os.getenv(name, '').split(',') if v.strip()}
    if required and not values:
        raise ConfigError(f'{name} fehlt (kommaseparierte numerische IDs)')
    if any(not (v.isascii() and v.isdigit()) or len(v) > 20 for v in values):
        raise ConfigError(f'{name}: nur kommaseparierte numerische IDs erlaubt')
    return values


def secret(*names, required=True):
    value = env(*names)
    raw = next((os.getenv(n, '') for n in names if os.getenv(n, '').strip()), '')
    if required and not value:
        raise ConfigError(f'{names[0]} fehlt')
    if raw and (raw != raw.strip() or any(c in raw for c in '\r\n')):
        raise ConfigError(f'{names[0]} enthält Leerzeichen oder Zeilenumbrüche am Rand')
    return value


def load_config():
    cfg = {
        'pushover_token': secret('PUSHOVER_TOKEN', 'PUSHOVER_API_TOKEN'),
        'pushover_user': secret('PUSHOVER_USER', 'PUSHOVER_USER_KEY'),
        'pushover_device': env('PUSHOVER_DEVICE'),
        'pushover_sound': env('PUSHOVER_SOUND'),
        'emergency_retry': integer('EMERGENCY_RETRY', 30, 30, 10800),
        'emergency_expire': integer('EMERGENCY_EXPIRE', 3600, 30, 10800),
        'burst_count': integer('BURST_COUNT', 10, 1, 50),
        'burst_delay': integer('BURST_DELAY', 2, 0, 60),
        'burst_priority': integer('BURST_PRIORITY', 1, -2, 1),
        'fail_threshold': integer('FAIL_ALERT_THRESHOLD', 5, 1, 1000),
        'fail_repeat': integer('FAIL_ALERT_REPEAT_MIN', 60, 1, 10080) * 60,
        'retention_days': integer('SENT_RETENTION_DAYS', 30, 1, 3650),
        'data': Path(env('DATA_DIR', 'STATE_DIR', default='/data')),
        'test_on_start': boolean('TEST_ON_START'),
    }
    if not cfg['pushover_user'].isalnum() or not cfg['pushover_token'].isalnum():
        raise ConfigError('PUSHOVER_TOKEN/PUSHOVER_USER dürfen nur Buchstaben und Ziffern enthalten')

    # Schalter: X_ENABLED / DISCORD_ENABLED=false deaktiviert eine Quelle, ohne ihre Werte zu löschen
    cfg['x_enabled'] = boolean('X_ENABLED', True)
    cfg['discord_enabled'] = boolean('DISCORD_ENABLED', True)
    accounts = [a.strip().lstrip('@') for a in env('X_ACCOUNTS', 'ACCOUNTS').split(',') if a.strip()]
    if not cfg['x_enabled']:
        accounts = []
    if any(not X_ACCOUNT_RE.match(a) for a in accounts):
        raise ConfigError('X_ACCOUNTS: nur X-Benutzernamen (A-Z, 0-9, _), kommasepariert')
    cfg['x'] = None
    if accounts:
        cfg['x'] = {
            'accounts': list(dict.fromkeys(accounts)),
            'mode': choice(('X_MODE', 'MODE'), 'emergency', MODES),
            'interval': integer(('X_POLL_INTERVAL', 'POLL_INTERVAL'), 60, 30, 86400),
            'include_rts': boolean(('X_INCLUDE_RETWEETS', 'INCLUDE_RETWEETS')),
            'include_replies': boolean(('X_INCLUDE_REPLIES', 'INCLUDE_REPLIES')),
            'rsshub_url': env('RSSHUB_URL', default='http://127.0.0.1:1200').rstrip('/'),
            'access_key': env('RSSHUB_ACCESS_KEY', 'ACCESS_KEY'),
            'timeout': integer('X_REQUEST_TIMEOUT', 30, 5, 300),
            'selfheal_after': integer('SELFHEAL_AFTER', 3, 0, 100),
            'selfheal_max': integer('SELFHEAL_MAX_PER_DAY', 3, 0, 100),
            'restart_flag': Path(env('RESTART_FLAG', default='/tmp/postalarm-restart-rsshub')),
        }
        if not urllib.parse.urlsplit(cfg['x']['rsshub_url']).scheme in ('http', 'https'):
            raise ConfigError('RSSHUB_URL muss mit http:// oder https:// beginnen')

    channels = ids('DISCORD_CHANNEL_IDS') if cfg['discord_enabled'] else set()
    cfg['discord'] = None
    if channels:
        secret('DISCORD_TOKEN')
        cfg['discord'] = {
            'channels': channels,
            'users': ids('DISCORD_USER_IDS', True),
            'guilds': ids('DISCORD_GUILD_IDS'),
            'mode': choice('DISCORD_MODE', 'normal', MODES),
            'interval': integer(('DISCORD_POLL_INTERVAL', 'POLL_INTERVAL_SECONDS'), 60, 30, 86400),
            'lookback': integer('INITIAL_LOOKBACK_SECONDS', 0, 0, 30 * 86400),
            'timeout': integer('EXPORT_TIMEOUT_SECONDS', 300, 30, 3600),
            'live': boolean('DISCORD_LIVE'),
            # Rückstand in Fenstern dieser Größe exportieren (verhindert Timeouts nach Pausen)
            'catchup_hours': integer('DISCORD_CATCHUP_HOURS', 2, 1, 168),
            # Nach einem Treffer: für active_duration Sekunden im active_interval abfragen
            'active_interval': integer('DISCORD_ACTIVE_INTERVAL', 600, 30, 86400),
            'active_duration': integer('DISCORD_ACTIVE_DURATION', 7200, 0, 7 * 86400),
            'live_dir': Path(env('DISCORD_ARCHIVE_DIR', default='/archive')),
        }
    elif env('DISCORD_USER_IDS') and cfg['discord_enabled']:
        raise ConfigError('DISCORD_USER_IDS gesetzt, aber DISCORD_CHANNEL_IDS fehlt')
    elif boolean('DISCORD_LIVE') and cfg['discord_enabled']:
        raise ConfigError('DISCORD_LIVE braucht DISCORD_CHANNEL_IDS (schreibt die Überwachungsexporte mit)')

    cfg['archive'] = None
    if boolean('DISCORD_ARCHIVE') and cfg['discord_enabled']:
        archive_channels = ids('DISCORD_ARCHIVE_CHANNEL_IDS') or channels
        if not archive_channels:
            raise ConfigError('DISCORD_ARCHIVE: DISCORD_ARCHIVE_CHANNEL_IDS oder DISCORD_CHANNEL_IDS setzen')
        secret('DISCORD_TOKEN')
        since = env('DISCORD_ARCHIVE_SINCE')
        if since:
            try:
                since_dt = dt.datetime.fromisoformat(since)
            except ValueError:
                raise ConfigError('DISCORD_ARCHIVE_SINCE muss ein Datum sein, z. B. 2026-01-01') from None
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=dt.timezone.utc)
            since = str(snowflake_at(since_dt))
        partition = env('DISCORD_ARCHIVE_PARTITION')
        if partition and not re.fullmatch(r'\d+(kb|mb|gb)?', partition.lower()):
            raise ConfigError('DISCORD_ARCHIVE_PARTITION: Nachrichtenzahl (z. B. 5000) oder Größe (z. B. 20mb)')
        cfg['archive'] = {
            'channels': archive_channels,
            'format': ARCHIVE_FORMATS[choice('DISCORD_ARCHIVE_FORMAT', 'htmldark', tuple(ARCHIVE_FORMATS))],
            'interval': integer('DISCORD_ARCHIVE_INTERVAL_HOURS', 24, 1, 720) * 3600,
            'media': boolean('DISCORD_ARCHIVE_MEDIA'),
            'since': since,
            'partition': partition.lower(),
            'timeout': integer('DISCORD_ARCHIVE_TIMEOUT', 7200, 60, 86400),
            'dir': Path(env('DISCORD_ARCHIVE_DIR', default='/archive')),
        }

    deliberately_off = not cfg['x_enabled'] or not cfg['discord_enabled']
    if not cfg['x'] and not cfg['discord'] and not cfg['archive'] and not deliberately_off:
        raise ConfigError('Keine Quelle konfiguriert: X_ACCOUNTS, DISCORD_CHANNEL_IDS oder DISCORD_ARCHIVE setzen')
    return cfg


# ================================================================ Hilfsfunktionen

def snowflake_at(when):
    return max(int(when.timestamp() * 1000) - DISCORD_EPOCH_MS, 0) << 22


def redact(text, secrets):
    for value in secrets:
        if value and len(value) >= 6:
            text = text.replace(value, '***')
    return text


def clip(text, limit):
    text = str(text)
    return text if len(text) <= limit else text[:limit - 1] + '…'


def clean(text):
    text = re.sub(r'<br\s*/?>', '\n', text or '', flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    return html.unescape(text).strip()


# ================================================================ Datenbank

def database(path):
    db = sqlite3.connect(path, timeout=30, check_same_thread=False)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=NORMAL')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS pending (id TEXT PRIMARY KEY, source TEXT NOT NULL,
            payload TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS sent (id TEXT PRIMARY KEY, source TEXT NOT NULL, ts INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS rejected (id TEXT PRIMARY KEY, reason TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS cursors (channel TEXT PRIMARY KEY, after TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS x_seen (account TEXT NOT NULL, id TEXT NOT NULL, ts INTEGER NOT NULL,
            PRIMARY KEY (account, id));
        CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    ''')
    db.commit()
    return db


def kv_get(db, key, default=None):
    row = db.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def kv_set(db, key, value):
    with db:
        db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)', (key, json.dumps(value)))


def known(db, item_id):
    return db.execute('SELECT 1 FROM sent WHERE id=? UNION ALL SELECT 1 FROM rejected WHERE id=? '
                      'UNION ALL SELECT 1 FROM pending WHERE id=?', (item_id, item_id, item_id)).fetchone()


def enqueue(db, item_id, source, payload):
    cur = db.execute('INSERT OR IGNORE INTO pending (id, source, payload) VALUES (?,?,?)',
                     (item_id, source, json.dumps(payload)))
    return cur.rowcount


def prune(db, retention_days):
    """Discord-IDs nur entfernen, wenn älter als die Aufbewahrung UND hinter jedem Kanal-Cursor.

    Exporte liefern nur Nachrichten nach dem Cursor, solche IDs kommen also nie wieder.
    X-Duplikatschutz läuft über x_seen (begrenzt je Account), sent dort ist nur Protokoll.
    """
    cutoff = snowflake_at(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=retention_days))
    for (after,) in db.execute('SELECT after FROM cursors').fetchall():
        if after.isdigit():
            cutoff = min(cutoff, int(after) + 1)
        else:
            try:
                cutoff = min(cutoff, snowflake_at(dt.datetime.fromisoformat(after)))
            except ValueError:
                return 0
    old = int(time.time()) - retention_days * 86400
    with db:
        removed = db.execute("DELETE FROM sent WHERE source='discord' AND "
                             "CAST(substr(id, 9) AS INTEGER) < ?", (cutoff,)).rowcount
        removed += db.execute("DELETE FROM sent WHERE source='x' AND ts < ?", (old,)).rowcount
        removed += db.execute("DELETE FROM rejected WHERE id LIKE 'discord:%' AND "
                              "CAST(substr(id, 9) AS INTEGER) < ?", (cutoff,)).rowcount
    if removed:
        log.info('%d alte Zustelleinträge entfernt', removed)
    return removed


# ================================================================ Pushover

def push(cfg, fields):
    data = {'token': cfg['pushover_token'], 'user': cfg['pushover_user'], **fields}
    if cfg['pushover_device']:
        data.setdefault('device', cfg['pushover_device'])
    if cfg['pushover_sound']:
        data.setdefault('sound', cfg['pushover_sound'])
    data['title'] = clip(data.get('title', 'postalarm'), LIMIT_TITLE)
    data['message'] = clip(data.get('message') or '-', LIMIT_MESSAGE)
    if 'url' in data and len(data['url']) > LIMIT_URL:
        data.pop('url')
        data.pop('url_title', None)
    if 'url_title' in data:
        data['url_title'] = clip(data['url_title'], LIMIT_URL_TITLE)
    request = urllib.request.Request(PUSHOVER_URL, data=urllib.parse.urlencode(data).encode(),
                                     headers={'User-Agent': 'postalarm'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read() or b'{}')
    except urllib.error.HTTPError as err:
        try:
            body = json.loads(err.read() or b'{}')
        except ValueError:
            body = {}
        detail = '; '.join(str(e) for e in body.get('errors', []))[:300]
        if err.code == 429:
            raise PushoverError('quota', 'Monatslimit erreicht') from None
        if 400 <= err.code < 500:
            # Ungültiges Token/Empfänger betrifft alle Nachrichten -> Warteschlange behalten.
            kind = 'config' if ('token' in body or 'user' in body) else 'message'
            raise PushoverError(kind, detail or f'HTTP {err.code}') from None
        raise PushoverError('temporary', f'HTTP {err.code}') from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as err:
        raise PushoverError('temporary', type(err).__name__) from None
    if body.get('status') != 1:
        raise PushoverError('temporary', 'unerwartete Antwort')
    return body


def alert_messages(cfg, mode, payload, start=0):
    """Pushover-Aufrufe für einen Treffer je nach Modus; ab `start` (für fortgesetzte Bursts)."""
    base = {'title': payload['title'], 'message': payload['message'],
            'url': payload['url'], 'url_title': payload['url_title']}
    if mode == 'emergency':
        return [{**base, 'priority': 2, 'retry': cfg['emergency_retry'],
                 'expire': cfg['emergency_expire'], 'tags': f"postalarm,{payload['source']}"}][start:]
    if mode == 'burst':
        n = cfg['burst_count']
        return [{**base, 'title': f"{payload['title']} ({i + 1}/{n})", 'priority': cfg['burst_priority']}
                for i in range(start, n)]
    return [{**base, 'priority': 1 if mode == 'high' else 0}][start:]


def source_mode(cfg, source):
    return (cfg.get(source) or {}).get('mode', 'normal')


def deliver(db, cfg, sender=push):
    """Arbeitet die Warteschlange ab. Gibt False zurück, wenn ein Wiederholungsversuch nötig ist."""
    with DELIVER_LOCK:
        rows = db.execute('SELECT id, source, payload, progress FROM pending ORDER BY rowid').fetchall()
        for item_id, source, raw, progress in rows:
            payload = json.loads(raw)
            mode = source_mode(cfg, source)
            try:
                for offset, fields in enumerate(alert_messages(cfg, mode, payload, progress)):
                    if offset and mode == 'burst':
                        if STOP.wait(cfg['burst_delay']):
                            return False
                    sender(cfg, fields)
                    progress += 1
                    with db:
                        db.execute('UPDATE pending SET progress=? WHERE id=?', (progress, item_id))
            except PushoverError as err:
                if err.kind == 'message':
                    # Einzelne unzustellbare Nachricht darf die Warteschlange nicht blockieren.
                    with db:
                        db.execute('INSERT OR IGNORE INTO rejected VALUES (?,?)', (item_id, err.detail))
                        db.execute('DELETE FROM pending WHERE id=?', (item_id,))
                    log.error('Pushover lehnt %s dauerhaft ab: %s', item_id, err.detail)
                    continue
                log.error('Pushover-Zustellung pausiert (%s); Warteschlange bleibt erhalten', err)
                return False
            with db:
                db.execute('INSERT OR IGNORE INTO sent VALUES (?,?,?)', (item_id, source, int(time.time())))
                db.execute('DELETE FROM pending WHERE id=?', (item_id,))
            log.info('Alarm zugestellt %s (Modus %s)', item_id, mode)
    return True


def notify(cfg, title, message, priority=0):
    """Statusmeldungen (Störung, Selbstheilung) best effort, ohne Warteschlange."""
    try:
        push(cfg, {'title': title, 'message': message, 'priority': priority})
    except PushoverError as err:
        log.error('Statusmeldung nicht zugestellt (%s)', err)


class FailureTracker:
    """Störungsalarm, wenn eine Quelle (Account/Kanal) mehrfach in Folge ausfällt."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.fails = {}
        self.last_alert = {}

    def failed(self, key, error):
        n = self.fails[key] = self.fails.get(key, 0) + 1
        now = time.time()
        if n >= self.cfg['fail_threshold'] and now - self.last_alert.get(key, 0) > self.cfg['fail_repeat']:
            notify(self.cfg, 'postalarm: Quelle gestört', f'{key}: {n} Fehlversuche in Folge.\n{error}', 1)
            self.last_alert[key] = now
        return n

    def ok(self, key):
        if self.fails.get(key, 0) >= self.cfg['fail_threshold']:
            notify(self.cfg, 'postalarm: Quelle wieder ok', f'{key} wird wieder gelesen.')
        self.fails[key] = 0
        self.last_alert.pop(key, None)


# ================================================================ X über RSSHub

def x_feed_url(x, account):
    flags = (f"includeRts={'true' if x['include_rts'] else 'false'}&"
             f"includeReplies={'true' if x['include_replies'] else 'false'}")
    query = {'format': 'json'}
    if x['access_key']:
        query['key'] = x['access_key']
    return f"{x['rsshub_url']}/twitter/user/{urllib.parse.quote(account)}/{flags}?{urllib.parse.urlencode(query)}"


def rsshub_error(body):
    """Fehlermeldung aus der RSSHub-Fehlerseite ziehen (<code class="... details ...">)."""
    match = re.search(r'<code[^>]*\bdetails\b[^>]*>(.*?)</code>', body or '', re.S | re.I)
    if match:
        return clean(match.group(1))[:600]
    return clean(re.sub(r'<(script|style)[^>]*>.*?</\1>', '', body or '', flags=re.S | re.I))[:300]


def fetch_x(x, account):
    request = urllib.request.Request(x_feed_url(x, account), headers={'User-Agent': 'postalarm'})
    try:
        with urllib.request.urlopen(request, timeout=x['timeout']) as response:
            doc = json.loads(response.read())
    except urllib.error.HTTPError as err:
        raise RuntimeError(f'HTTP {err.code}: {rsshub_error(err.read().decode("utf-8", "replace"))}') from None
    except urllib.error.URLError as err:
        raise RuntimeError(f'RSSHub nicht erreichbar ({type(err.reason).__name__})') from None
    except ValueError:
        raise RuntimeError('RSSHub lieferte kein gültiges JSON') from None
    items = doc.get('items')
    if not isinstance(items, list):
        raise RuntimeError('RSSHub-Antwort ohne items')
    return [i for i in items if isinstance(i, dict) and (i.get('id') or i.get('url'))]


def item_id(item):
    return str(item.get('id') or item.get('url'))


def ingest_x(db, account, items):
    """Neue Tweets einreihen. Erstlauf je Account markiert nur als gesehen."""
    now = int(time.time())
    first = not kv_get(db, f'x_init:{account}')
    count = 0
    with db:
        new = [i for i in items
               if not db.execute('SELECT 1 FROM x_seen WHERE account=? AND id=?', (account, item_id(i))).fetchone()]
        for item in reversed(new):  # Feeds sind neueste zuerst -> älteste zuerst alarmieren
            tid = item_id(item)
            db.execute('INSERT OR IGNORE INTO x_seen VALUES (?,?,?)', (account, tid, now))
            if first:
                continue
            payload = {'source': 'x', 'title': f'@{account} hat getwittert',
                       'message': clean(item.get('content_html') or item.get('content_text') or item.get('title')),
                       'url': item.get('url') or f'https://x.com/{account}', 'url_title': 'Tweet öffnen'}
            count += enqueue(db, f'x:{account}:{tid}', 'x', payload)
        db.execute('DELETE FROM x_seen WHERE account=? AND id NOT IN (SELECT id FROM x_seen WHERE account=? '
                   'ORDER BY ts DESC, rowid DESC LIMIT ?)', (account, account, X_KEEP_SEEN))
    if first:
        kv_set(db, f'x_init:{account}', True)
        log.info('@%s: Erstlauf, %d bestehende Tweets als gesehen markiert', account, len(new))
    return count


def maybe_selfheal(db, cfg, account, fails, error):
    """Nach mehreren Twitter-API-404 RSSHub neu starten lassen (frische GraphQL-Query-IDs)."""
    x = cfg['x']
    if x['selfheal_after'] <= 0 or 'Twitter API error: 404' not in str(error) or fails % x['selfheal_after']:
        return False
    now = time.time()
    history = [t for t in kv_get(db, 'selfheal', []) if now - t < 86400]
    if len(history) >= x['selfheal_max']:
        log.warning('Selbstheilung übersprungen: Limit %d/24h erreicht', x['selfheal_max'])
        return False
    try:
        x['restart_flag'].touch()
    except OSError as err:
        log.error('Restart-Flag nicht schreibbar: %s', err)
        return False
    history.append(now)
    kv_set(db, 'selfheal', history)
    log.warning('@%s: %d x 404 in Folge, starte RSSHub neu (%d/%d in 24h)',
                account, fails, len(history), x['selfheal_max'])
    notify(cfg, 'postalarm: Selbstheilung', f'@{account}: {fails} x Twitter-API-404. RSSHub wird neu '
           f'gestartet ({len(history)}/{x["selfheal_max"]} in 24 h).')
    return True


def poll_x(db, cfg, tracker):
    ok = True
    for account in cfg['x']['accounts']:
        if STOP.is_set():
            break
        key = f'X @{account}'
        try:
            count = ingest_x(db, account, fetch_x(cfg['x'], account))
            tracker.ok(key)
            log.log(logging.INFO if count else logging.DEBUG, '@%s: %d neue Tweets', account, count)
        except Exception as exc:
            ok = False
            message = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
            fails = tracker.failed(key, message)
            log.warning('@%s: Abruf fehlgeschlagen (%d in Folge): %s', account, fails, message)
            maybe_selfheal(db, cfg, account, fails, message)
    return ok


# ================================================================ Discord über DiscordChatExporter

def dce_env():
    env_ = {k: os.environ[k] for k in DCE_ENV if k in os.environ}
    env_['FUCK_RUSSIA'] = 'true'  # DCE-Option: Konsolenbanner unterdrücken
    return env_


def dce_error(stderr):
    """Aussagekräftige Fehlerzeilen aus der DCE-Ausgabe: ohne Stacktrace ("at ..."), Token geschwärzt."""
    lines = [line.strip() for line in stderr.splitlines()]
    lines = [line for line in lines if line and not line.startswith('at ') and not line.startswith('---')]
    if not lines:
        return 'keine Ausgabe'
    # Exception-Zeilen bevorzugen, sonst die letzten Zeilen
    picked = [line for line in lines if 'exception' in line.lower() or 'error' in line.lower()] or lines
    return redact(' | '.join(dict.fromkeys(picked[-3:])), [os.getenv('DISCORD_TOKEN', '')])[:500]


def run_dce(args, timeout, stderr_path):
    command = [os.getenv('DCE_EXECUTABLE', '/opt/dce/DiscordChatExporter.Cli'), *args]
    with open(stderr_path, 'wb') as stderr:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=stderr, env=dce_env(), start_new_session=True)
        deadline = time.monotonic() + timeout
        while True:
            try:
                return proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                if STOP.is_set() or time.monotonic() > deadline:
                    proc.kill()
                    proc.wait()
                    raise TimeoutError('Export abgebrochen' if STOP.is_set() else 'Export-Timeout') from None


def export(channel, after, workdir, timeout=300, before=None):
    workdir = Path(workdir)
    output, errors = workdir / 'messages.json', workdir / 'stderr.log'
    # Token nur über die Umgebung, nie als Argument (sichtbar in ps/docker top).
    args = ['export', '-c', channel, '-f', 'Json', '-o', str(output), '--after', after,
            '--before', before or dt.datetime.now(dt.timezone.utc).isoformat(), '--parallel', '1']
    code = run_dce(args, timeout, errors)
    if code:
        raise RuntimeError(f"DiscordChatExporter exit={code}: "
                           f"{dce_error(errors.read_text(errors='replace') if errors.exists() else '')}")
    with output.open(encoding='utf-8') as f:
        doc = json.load(f)
    if str(doc['channel']['id']) != channel:
        raise ValueError('Export enthält unerwarteten Kanal')
    return doc


def ingest_discord(db, doc, users, channels, guilds):
    channel = str(doc['channel']['id'])
    guild = str(doc['guild']['id'])
    if (channels and channel not in channels) or (guilds and guild not in guilds):
        return 0
    count = 0
    with db:
        for message in doc['messages']:
            mid = str(message['id'])
            author = message.get('author') or {}
            if str(author.get('id')) not in users or known(db, f'discord:{mid}'):
                continue
            name = author.get('nickname') or author.get('name') or author.get('id')
            payload = {'source': 'discord',
                       'title': f"{name} in #{doc['channel'].get('name') or channel}",
                       'message': f"{message_time(message):%d.%m. %H:%M}\n"
                                  f"{message.get('content') or '[Anhang oder Nachricht ohne Text]'}",
                       'url': f'https://discord.com/channels/{guild if guild != "0" else "@me"}/{channel}/{mid}',
                       'url_title': 'Nachricht öffnen'}
            count += enqueue(db, f'discord:{mid}', 'discord', payload)
    return count


def safe_name(name):
    """Datei-/Ordnername aus Server- oder Kanalnamen (wie bei DiscordChatExporter ohne Sonderzeichen)."""
    name = re.sub(r'[\x00-\x1f/\\:*?"<>|]', '_', str(name or '')).strip(' .')
    return name[:100] or '_'


def message_time(message):
    try:
        return dt.datetime.fromisoformat(message['timestamp']).astimezone()
    except (KeyError, TypeError, ValueError):
        ms = (int(message['id']) >> 22) + DISCORD_EPOCH_MS
        return dt.datetime.fromtimestamp(ms / 1000).astimezone()


def format_live(message):
    """Eine Nachricht als lesbarer Textblock (Felder laut DCE JsonMessageWriter)."""
    author = message.get('author') or {}
    name, nick = author.get('name') or author.get('id') or '?', author.get('nickname')
    who = f'{nick} (@{name})' if nick and nick != name else name
    head = f"[{message_time(message):%Y-%m-%d %H:%M:%S}] {who} #{message['id']}"
    reference = message.get('reference') or {}
    if reference.get('messageId'):
        head += f" ↪ #{reference['messageId']}"
    if message.get('timestampEdited'):
        head += ' (bearbeitet)'
    if author.get('isBot'):
        head += ' [Bot]'
    lines = [f'  {line}' for line in (message.get('content') or '').splitlines()]
    forwarded = message.get('forwardedMessage') or {}
    if forwarded.get('content'):
        lines += ['  [Weitergeleitet]'] + [f'    {line}' for line in forwarded['content'].splitlines()]
    for item in (message.get('attachments') or []) + (forwarded.get('attachments') or []):
        lines.append(f"  [Anhang] {item.get('fileName', '')} {item.get('url', '')}".rstrip())
    for embed in message.get('embeds') or []:
        text = ' '.join(x for x in (embed.get('title'), embed.get('url')) if x)
        if text:
            lines.append(f'  [Link] {text}')
    for sticker in message.get('stickers') or []:
        lines.append(f"  [Sticker] {sticker.get('name', '')}")
    return '\n'.join([head] + (lines or ['  [ohne Text]'])) + '\n'


def live_append(db, cfg, doc):
    """Neue Nachrichten (alle User) an Tagesdateien anhängen. True = geschrieben bzw. nichts zu tun.

    Duplikatschutz über die zuletzt geschriebene Message-ID je Kanal, damit ein erneuter Export
    nach einem Fehler nichts doppelt schreibt.
    """
    channel = str(doc['channel']['id'])
    key = f'live:{channel}'
    last = int(kv_get(db, key, 0))
    messages = sorted((m for m in doc['messages'] if int(m['id']) > last), key=lambda m: int(m['id']))
    if not messages:
        return True
    folder = cfg['discord']['live_dir'] / safe_name(doc['guild'].get('name')) / safe_name(
        doc['channel'].get('name') or channel) / 'live'
    try:
        folder.mkdir(parents=True, exist_ok=True)
        by_day = {}
        for message in messages:
            by_day.setdefault(f'{message_time(message):%Y-%m-%d}', []).append(message)
        for day, items in by_day.items():
            path = folder / f'{day}.txt'
            header = '' if path.exists() else (
                f"# {doc['guild'].get('name', '')} / #{doc['channel'].get('name', '')} "
                f"(Kanal {channel}) – {day}\n# Format: [Zeit] Name (@user) #MessageID ↪ #Antwort-auf\n\n")
            with open(path, 'a', encoding='utf-8') as f:
                f.write(header + ''.join(format_live(m) for m in items))
                f.flush()
                os.fsync(f.fileno())
    except OSError as err:
        log.error('Live-Mitschnitt Kanal %s fehlgeschlagen (%s); wird beim nächsten Abruf nachgeholt',
                  channel, type(err).__name__)
        return False
    kv_set(db, key, str(max(int(m['id']) for m in messages)))
    return True


def cursor_time(after):
    """Zeitpunkt eines Cursors (Message-ID oder ISO-Datum)."""
    if after.isdigit():
        return dt.datetime.fromtimestamp(((int(after) >> 22) + DISCORD_EPOCH_MS) / 1000, dt.timezone.utc)
    when = dt.datetime.fromisoformat(after)
    return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)


def poll_channel(db, cfg, channel, now=None):
    """Exportiert neue Nachrichten. Großer Rückstand wird in Zeitfenstern abgearbeitet, damit ein
    einzelner Export nie zu groß wird (sonst Timeout -> Cursor bleibt stehen -> Rückstand wächst)."""
    d = cfg['discord']
    window = dt.timedelta(hours=d['catchup_hours'])
    total = 0
    for _ in range(48):
        after = db.execute('SELECT after FROM cursors WHERE channel=?', (channel,)).fetchone()[0]
        current = now or dt.datetime.now(dt.timezone.utc)
        end = cursor_time(after) + window
        capped = end < current - dt.timedelta(minutes=1)
        before = str(snowflake_at(end)) if capped else None
        hits, progressed = poll_window(db, cfg, channel, after, before)
        total += hits
        if not capped or not progressed or STOP.is_set():
            break
    return total


def poll_window(db, cfg, channel, after, before):
    d = cfg['discord']
    with tempfile.TemporaryDirectory(prefix='pa-') as tmp:
        doc = export(channel, after, tmp, d['timeout'], before)
    messages = doc['messages']
    # Live-Mitschnitt zuerst; schlägt er fehl, bleibt der Cursor stehen und der Bereich wird erneut
    # exportiert. Alarme laufen trotzdem (Duplikatschutz), der Mitschnitt holt nach.
    live_ok = live_append(db, cfg, doc) if d['live'] else True
    with db:
        count = ingest_discord(db, doc, d['users'], d['channels'], d['guilds'])
        # Cursor nur auf empfangene Nachricht setzen, nie auf Uhrzeit: keine Lücken bei Verzögerung.
        new_after = None
        if messages and live_ok:
            newest = max(int(m['id']) for m in messages)
            if not after.isdigit() or newest > int(after):
                new_after = newest
        if before and live_ok:
            # Fenster vollständig exportiert: bis zum Fensterende weiterrücken, auch ohne Nachrichten
            new_after = max(new_after or 0, int(before) - 1)
        if new_after is not None:
            db.execute('UPDATE cursors SET after=? WHERE channel=?', (str(new_after), channel))
    if before:
        log.info('Discord-Kanal %s: Rückstand bis %s: %d Nachrichten, %d neue Treffer', channel,
                 cursor_time(before).astimezone().strftime('%d.%m. %H:%M'), len(messages), count)
    else:
        log.info('Discord-Kanal %s: %d Nachrichten, %d neue Treffer', channel, len(messages), count)
    return count, new_after is not None


AUTH_INVALID_MARKERS = ('Authentication token is invalid', 'Unauthorized')


def poll_discord(db, cfg, tracker):
    ok = True
    d = cfg['discord']
    if d.get('auth_invalid'):
        # Ungültiger Token: keine weiteren Anfragen an Discord bis zum Neustart (neuer Token per Apply)
        return True
    for channel in sorted(d['channels']):
        if STOP.is_set():
            break
        key = f'Discord-Kanal {channel}'
        try:
            hits = poll_channel(db, cfg, channel)
            tracker.ok(key)
        except Exception as exc:
            ok = False
            message = str(exc) if isinstance(exc, (RuntimeError, TimeoutError)) else type(exc).__name__
            if any(marker in message for marker in AUTH_INVALID_MARKERS):
                d['auth_invalid'] = True
                log.error('Discord-Token ungültig: Discord pausiert bis zum nächsten Neustart/Apply')
                notify(cfg, 'postalarm: Discord-Token ungültig',
                       'Discord wird nicht mehr abgefragt. Neuen Token in DISCORD_TOKEN eintragen und Apply. '
                       'Nachrichten seit dem letzten Abruf werden danach nachgeholt.', 1)
                return True
            fails = tracker.failed(key, message)
            log.error('%s fehlgeschlagen (%d in Folge): %s', key, fails, message)
            continue
        if hits and d['active_duration'] and d['active_interval'] < d['interval']:
            until = time.time() + d['active_duration']
            if until > kv_get(db, 'discord_active_until', 0):
                kv_set(db, 'discord_active_until', until)
            log.info('Treffer in Kanal %s: %d min lang alle %d min abfragen', channel,
                     d['active_duration'] // 60, d['active_interval'] // 60)
    return ok


def discord_interval(db, cfg):
    """Normales Intervall, nach einem Treffer für eine Weile das kürzere (persistiert über Neustarts)."""
    d = cfg['discord']
    if time.time() < kv_get(db, 'discord_active_until', 0):
        return min(d['active_interval'], d['interval'])
    return d['interval']


# ================================================================ Discord-Archiv (ganze Kanäle)

def archive_channel(db, cfg, channel, now=None):
    """Exportiert einen Kanal inkrementell ins Archiv. Gibt die Zahl der geschriebenen Dateien zurück.

    Zeitraum: (letzter Stand, jetzt - 60 s]. Grenzen sind Discord-Snowflakes, damit Folgeläufe
    lückenlos und ohne Überschneidung aneinander anschließen.
    """
    a = cfg['archive']
    now = now or dt.datetime.now(dt.timezone.utc)
    key = f'archive:{channel}'
    after = kv_get(db, key) or a['since']
    before = str(snowflake_at(now - dt.timedelta(seconds=60)))
    if after and int(after) >= int(before):
        return 0
    cli_format, ext = a['format']
    stamp = now.astimezone().strftime('%Y-%m-%d_%H%M%S')
    a['dir'].mkdir(parents=True, exist_ok=True)
    # %G/%C: Server- und Kanalname, setzt DiscordChatExporter ein (Using-the-CLI.md)
    args = ['export', '-c', channel, '-f', cli_format, '-o', str(a['dir'] / '%G' / '%C' / f'{stamp}.{ext}'),
            '--before', before, '--parallel', '1']
    if after:
        args += ['--after', after]
    if a['partition']:
        args += ['--partition', a['partition']]
    if a['media']:
        args += ['--media', '--reuse-media', '--media-dir', str(a['dir'] / '_media') + '/']

    def written():
        return [p for p in a['dir'].glob(f'*/*/{stamp}*.{ext}') if p.is_file()]

    with tempfile.TemporaryDirectory(prefix='pa-archive-') as tmp:
        errors = Path(tmp) / 'stderr.log'
        try:
            code = run_dce(args, a['timeout'], errors)
        except TimeoutError:
            for p in written():
                p.unlink()
            raise
        stderr = errors.read_text(errors='replace') if errors.exists() else ''
    files = written()
    if code:
        for p in files:
            p.unlink()
        raise RuntimeError(f'DiscordChatExporter exit={code}: {dce_error(stderr)}')
    if DCE_EMPTY_MARKER in stderr:
        for p in files:
            p.unlink()
        files = []
    kv_set(db, key, before)
    if files:
        log.info('Archiv Kanal %s: %s', channel, ', '.join(str(p.relative_to(a['dir'])) for p in files))
    else:
        log.info('Archiv Kanal %s: keine neuen Nachrichten', channel)
    return len(files)


def poll_archive(db, cfg, tracker):
    ok = True
    for channel in sorted(cfg['archive']['channels']):
        if STOP.is_set():
            break
        key = f'Discord-Archiv {channel}'
        try:
            archive_channel(db, cfg, channel)
            tracker.ok(key)
        except Exception as exc:
            ok = False
            message = str(exc) if isinstance(exc, (RuntimeError, TimeoutError)) else type(exc).__name__
            fails = tracker.failed(key, message)
            log.error('%s fehlgeschlagen (%d in Folge): %s', key, fails, message)
    return ok


# ================================================================ Health / Selbsttest

def write_heartbeat(data, name, deadline):
    tmp = data / f'heartbeat-{name}.tmp'
    tmp.write_text(str(int(deadline)))
    tmp.replace(data / f'heartbeat-{name}')


def healthcheck():
    data = Path(env('DATA_DIR', 'STATE_DIR', default='/data'))
    beats = sorted(b for b in data.glob('heartbeat-*') if not b.name.endswith('.tmp'))
    if not beats:
        print('kein Heartbeat')
        return 1
    for beat in beats:
        try:
            deadline = int(beat.read_text())
        except (OSError, ValueError):
            print(f'{beat.name} unlesbar')
            return 1
        if time.time() > deadline:
            print(f'{beat.name} überfällig')
            return 1
    return 0


def self_test():
    """Prüft das fertige Image: CA-Zertifikate, SQLite, Schreibrechte, DCE-Start."""
    data = Path(env('DATA_DIR', 'STATE_DIR', default='/data'))
    paths = ssl.get_default_verify_paths()
    has_ca = ssl.create_default_context().cert_store_stats().get('x509_ca') or \
        (paths.capath and any(Path(paths.capath).glob('*.0')))
    if not has_ca:
        raise ConfigError('Selbsttest: keine CA-Zertifikate gefunden')
    with tempfile.TemporaryDirectory(dir=data) as tmp:
        database(Path(tmp) / 'test.sqlite3').close()
        errors = Path(tmp) / 'stderr.log'
        try:
            code = run_dce(['--help'], 60, errors)
        except OSError as err:
            raise ConfigError(f'Selbsttest: DiscordChatExporter nicht ausführbar ({err})') from None
        if code != 0:
            tail = ' | '.join(errors.read_text(errors='replace').strip().splitlines()[-3:])[:500]
            raise ConfigError(f'Selbsttest: DiscordChatExporter startet nicht (exit={code}): {tail}')
    print(f'ok: python {sys.version.split()[0]}, {ssl.OPENSSL_VERSION}, sqlite {sqlite3.sqlite_version}, '
          f'uid={os.getuid()} gid={os.getgid()}')
    return 0


def test_alert(cfg, source):
    """Testalarm über den echten Alarmweg der Quelle (Emergency mit kurzer Laufzeit)."""
    payload = {'source': source, 'title': f'[TEST] postalarm {source}',
               'message': 'Testalarm. Wenn du das liest, funktioniert die Alarmkette.',
               'url': 'https://pushover.net', 'url_title': 'Pushover'}
    mode = source_mode(cfg, source)
    for i, fields in enumerate(alert_messages({**cfg, 'emergency_expire': 120}, mode, payload)):
        if i and mode == 'burst':
            time.sleep(cfg['burst_delay'])
        push(cfg, fields)
    log.info('Testalarm %s gesendet (Modus %s)', source, mode)


# ================================================================ Hauptschleife

def worker(cfg, name, poll, interval, budget):
    db = database(cfg['data'] / 'postalarm.sqlite3')
    tracker = FailureTracker(cfg)
    failures, last_prune = 0, 0.0
    write_heartbeat(cfg['data'], name, time.time() + budget)
    try:
        while not STOP.is_set():
            failed = not poll(db, cfg, tracker)
            try:
                failed |= not deliver(db, cfg)
            except Exception as exc:
                failed = True
                log.error('Zustellung fehlgeschlagen (%s); Warteschlange bleibt erhalten', type(exc).__name__)
            if name == 'discord' and time.time() - last_prune > 86400:
                prune(db, cfg['retention_days'])
                last_prune = time.time()
            failures = min(failures + 1, 4) if failed else 0
            # Erfolg: normales Intervall. Fehler: Backoff, höchstens 15 min (auch beim Archiv).
            base = interval(db, cfg) if callable(interval) else interval
            wait = min(min(base, 300) * 2 ** failures, 900) if failures else base
            write_heartbeat(cfg['data'], name, time.time() + wait + budget)
            STOP.wait(wait)
    except Exception as exc:
        log.error('%s-Thread abgestürzt (%s); Container wird beendet', name, type(exc).__name__)
        CRASHED.set()
        STOP.set()
        raise
    finally:
        db.close()


def main():
    logging.basicConfig(level=env('LOG_LEVEL', default='INFO').upper(),
                        format='%(asctime)s %(levelname)s %(message)s', stream=sys.stdout)
    cfg = load_config()
    data = cfg['data']
    data.mkdir(parents=True, exist_ok=True)
    if os.getuid() == 0:
        log.warning('Läuft als root; PUID/PGID im Container setzen')
    lock = (data / 'postalarm.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise ConfigError(f'{data} wird bereits von einer anderen Instanz verwendet') from None
    for stale in data.glob('heartbeat-*'):
        stale.unlink()
    db = database(data / 'postalarm.sqlite3')
    if cfg['discord']:
        initial = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=cfg['discord']['lookback'])).isoformat()
        with db:
            for channel in cfg['discord']['channels']:
                db.execute('INSERT OR IGNORE INTO cursors VALUES (?,?)', (channel, initial))
    db.close()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: STOP.set())

    threads = []
    if cfg['x']:
        x = cfg['x']
        log.info('X: %s via %s, Intervall %ds, Modus %s', ', '.join('@' + a for a in x['accounts']),
                 x['rsshub_url'], x['interval'], x['mode'])
        threads.append(('x', poll_x, x['interval'], len(x['accounts']) * x['timeout'] + 300))
    if cfg['discord']:
        d = cfg['discord']
        log.info('Discord: %d Benutzer, %d Kanäle, Intervall %ds, Modus %s, Live-Mitschnitt %s',
                 len(d['users']), len(d['channels']), d['interval'], d['mode'],
                 f"nach {d['live_dir']}" if d['live'] else 'aus')
        if d['live'] and not os.path.ismount(d['live_dir']):
            log.warning('%s ist kein gemountetes Verzeichnis: Mitschnitt geht beim Neuerstellen verloren', d['live_dir'])
        if d['active_duration'] and d['active_interval'] < d['interval']:
            log.info('Discord: nach Treffer %d min lang alle %d min', d['active_duration'] // 60,
                     d['active_interval'] // 60)
        threads.append(('discord', poll_discord, discord_interval, len(d['channels']) * d['timeout'] + 300))
    if cfg['archive']:
        a = cfg['archive']
        log.info('Discord-Archiv: %d Kanäle, alle %dh, Format %s, Medien %s, nach %s',
                 len(a['channels']), a['interval'] // 3600, a['format'][0], 'an' if a['media'] else 'aus', a['dir'])
        if not os.path.ismount(a['dir']):
            log.warning('%s ist kein gemountetes Verzeichnis: Archiv geht beim Neuerstellen des Containers verloren', a['dir'])
        threads.append(('archive', poll_archive, a['interval'], len(a['channels']) * a['timeout'] + 300))
    if cfg['test_on_start']:
        for name, *_ in [t for t in threads if t[0] in ('x', 'discord')]:
            try:
                test_alert(cfg, name)
            except PushoverError as err:
                log.error('Testalarm %s fehlgeschlagen (%s)', name, err)
    if not cfg['x_enabled']:
        log.info('X deaktiviert (X_ENABLED=false)')
    if not cfg['discord_enabled']:
        log.info('Discord deaktiviert (DISCORD_ENABLED=false)')
    if not threads:
        # Alles abgeschaltet: Container bleibt gesund im Leerlauf statt in einer Neustartschleife
        log.warning('Keine aktive Quelle, postalarm wartet im Leerlauf')
        while not STOP.is_set():
            write_heartbeat(data, 'idle', time.time() + 3600)
            STOP.wait(600)
        log.info('Beendet')
        lock.close()
        return 0
    running = [threading.Thread(target=worker, args=(cfg, *t), name=t[0], daemon=True) for t in threads]
    for t in running:
        t.start()
    while not STOP.is_set() and all(t.is_alive() for t in running):
        STOP.wait(1)
    STOP.set()
    for t in running:
        t.join(timeout=30)
    log.info('Beendet')
    lock.close()
    return 1 if CRASHED.is_set() or any(t.is_alive() for t in running) else 0


def cli(argv):
    if argv[1:2] == ['--healthcheck']:
        return healthcheck()
    try:
        if argv[1:2] == ['--self-test']:
            return self_test()
        if argv[1:2] == ['--test-alert']:
            logging.basicConfig(level='INFO', format='%(asctime)s %(levelname)s %(message)s', stream=sys.stdout)
            cfg = load_config()
            source = (argv[2:3] or [''])[0]
            if source not in ('x', 'discord') or not cfg[source]:
                raise ConfigError('Aufruf: --test-alert x|discord (Quelle muss konfiguriert sein)')
            test_alert(cfg, source)
            return 0
        return main()
    except ConfigError as exc:
        logging.error('Konfigurationsfehler: %s', exc)
    except PushoverError as exc:
        logging.error('Pushover-Fehler: %s', exc)
    except Exception as exc:
        logging.error('Start fehlgeschlagen (%s): Konfiguration und Datenverzeichnis prüfen', type(exc).__name__)
    return 1


if __name__ == '__main__':
    raise SystemExit(cli(sys.argv))
