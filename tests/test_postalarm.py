import datetime as dt
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse

import postalarm as pa

DOC = {'guild': {'id': '10'}, 'channel': {'id': '20', 'name': 'general'},
       'messages': [{'id': '100', 'author': {'id': '30', 'name': 'Alice'},
                     'timestamp': '2026-10-26T12:00:00Z', 'content': 'Hallo'}]}
ENV = {'PUSHOVER_TOKEN': 'testapp', 'PUSHOVER_USER': 'testuser',
       'DISCORD_TOKEN': 'discord-secret-token', 'DISCORD_USER_IDS': '30', 'DISCORD_CHANNEL_IDS': '20',
       'X_ACCOUNTS': 'gezine_dev'}
CLEAN_ENV = {k: '' for k in ('ACCOUNTS', 'MODE', 'POLL_INTERVAL', 'PUSHOVER_API_TOKEN', 'PUSHOVER_USER_KEY',
                             'X_MODE', 'DISCORD_MODE', 'STATE_DIR', 'DATA_DIR', 'ACCESS_KEY')}


def http_error(code, body):
    return urllib.error.HTTPError(pa.PUSHOVER_URL, code, 'err', {}, io.BytesIO(json.dumps(body).encode()))


def fake_dce(directory, script):
    path = Path(directory) / 'fake-dce'
    path.write_text(f'#!{sys.executable}\n{script}')
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def config(**overrides):
    with patch.dict(os.environ, {**CLEAN_ENV, **ENV, **overrides}):
        return pa.load_config()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name + '/state.db'
        self.db = pa.database(self.path)
        pa.STOP.clear()
        pa.CRASHED.clear()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def reopen(self):
        self.db.close()
        self.db = pa.database(self.path)


class ConfigTests(Base):
    def test_defaults_per_source(self):
        cfg = config()
        self.assertEqual(cfg['x']['mode'], 'emergency')
        self.assertEqual(cfg['discord']['mode'], 'normal')
        self.assertEqual(cfg['x']['accounts'], ['gezine_dev'])

    def test_legacy_aliases(self):
        cfg = config(PUSHOVER_TOKEN='', PUSHOVER_USER='', PUSHOVER_API_TOKEN='a1', PUSHOVER_USER_KEY='u1',
                     X_ACCOUNTS='', ACCOUNTS='@foo', MODE='burst')
        self.assertEqual((cfg['pushover_token'], cfg['pushover_user']), ('a1', 'u1'))
        self.assertEqual(cfg['x']['accounts'], ['foo'])
        self.assertEqual(cfg['x']['mode'], 'burst')

    def test_single_source(self):
        self.assertIsNone(config(X_ACCOUNTS='')['x'])
        self.assertIsNone(config(DISCORD_CHANNEL_IDS='', DISCORD_USER_IDS='')['discord'])

    def test_invalid(self):
        cases = [('X_ACCOUNTS', 'bad name'), ('X_MODE', 'loud'), ('DISCORD_USER_IDS', 'abc'),
                 ('DISCORD_POLL_INTERVAL', '5'), ('EMERGENCY_RETRY', '10'), ('BURST_COUNT', '99'),
                 ('PUSHOVER_USER', ' key '), ('DISCORD_TOKEN', ''), ('X_INCLUDE_RETWEETS', 'maybe'),
                 ('RSSHUB_URL', 'ftp://x')]
        for key, value in cases:
            with self.assertRaises(pa.ConfigError, msg=key) as ctx:
                config(**{key: value})
            self.assertNotIn('discord-secret-token', str(ctx.exception))
        with self.assertRaises(pa.ConfigError):
            config(X_ACCOUNTS='', DISCORD_CHANNEL_IDS='', DISCORD_USER_IDS='')


class AlertTests(Base):
    PAYLOAD = {'source': 'x', 'title': 'T', 'message': 'M', 'url': 'https://x.com/a', 'url_title': 'Tweet'}

    def test_modes(self):
        cfg = config(BURST_COUNT='3')
        emergency = pa.alert_messages(cfg, 'emergency', self.PAYLOAD)
        self.assertEqual(len(emergency), 1)
        self.assertEqual((emergency[0]['priority'], emergency[0]['retry'], emergency[0]['expire']), (2, 30, 3600))
        burst = pa.alert_messages(cfg, 'burst', self.PAYLOAD)
        self.assertEqual([m['title'] for m in burst], ['T (1/3)', 'T (2/3)', 'T (3/3)'])
        self.assertEqual(len(pa.alert_messages(cfg, 'burst', self.PAYLOAD, 2)), 1)
        self.assertEqual(pa.alert_messages(cfg, 'high', self.PAYLOAD)[0]['priority'], 1)
        self.assertEqual(pa.alert_messages(cfg, 'normal', self.PAYLOAD)[0]['priority'], 0)

    def test_mode_is_per_source(self):
        cfg = config(X_MODE='emergency', DISCORD_MODE='normal', BURST_DELAY='0')
        with self.db:
            pa.enqueue(self.db, 'x:a:1', 'x', {**self.PAYLOAD})
            pa.enqueue(self.db, 'discord:1', 'discord', {**self.PAYLOAD, 'source': 'discord'})
        sent = []
        self.assertTrue(pa.deliver(self.db, cfg, lambda c, f: sent.append(f)))
        self.assertEqual([f['priority'] for f in sent], [2, 0])

    def test_burst_resumes_after_failure(self):
        cfg = config(X_MODE='burst', BURST_COUNT='4', BURST_DELAY='0')
        with self.db:
            pa.enqueue(self.db, 'x:a:1', 'x', self.PAYLOAD)
        sent = []

        def flaky(_, fields):
            if len(sent) == 2 and not getattr(flaky, 'failed', False):
                flaky.failed = True
                raise pa.PushoverError('temporary', 'HTTP 503')
            sent.append(fields['title'])
        self.assertFalse(pa.deliver(self.db, cfg, flaky))
        self.reopen()
        self.assertTrue(pa.deliver(self.db, cfg, flaky))
        self.assertEqual(sent, ['T (1/4)', 'T (2/4)', 'T (3/4)', 'T (4/4)'])

    def test_rejected_message_does_not_block_queue(self):
        cfg = config(X_MODE='normal')
        with self.db:
            pa.enqueue(self.db, 'x:a:1', 'x', {**self.PAYLOAD, 'message': 'bad'})
            pa.enqueue(self.db, 'x:a:2', 'x', self.PAYLOAD)
        sent = []

        def sender(_, fields):
            if fields['message'] == 'bad':
                raise pa.PushoverError('message', 'message cannot be blank')
            sent.append(fields)
        self.assertTrue(pa.deliver(self.db, cfg, sender))
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.db.execute('SELECT id FROM rejected').fetchall(), [('x:a:1',)])

    def test_config_error_keeps_queue(self):
        cfg = config()
        with self.db:
            pa.enqueue(self.db, 'x:a:1', 'x', self.PAYLOAD)

        def sender(*_):
            raise pa.PushoverError('config', 'application token is invalid')
        self.assertFalse(pa.deliver(self.db, cfg, sender))
        self.assertEqual(self.db.execute('SELECT count(*) FROM pending').fetchone()[0], 1)

    def test_pushover_request(self):
        cfg = config(PUSHOVER_DEVICE='iphone', PUSHOVER_SOUND='siren')
        with patch('urllib.request.urlopen') as request:
            request.return_value.__enter__.return_value.read.return_value = b'{"status":1}'
            pa.push(cfg, {'title': 'T' * 400, 'message': 'x' * 5000, 'url': 'u' * 600, 'url_title': 'L'})
            sent = request.call_args.args[0]
            fields = dict(urllib.parse.parse_qsl(sent.data.decode()))
        self.assertEqual(sent.full_url, 'https://api.pushover.net/1/messages.json')
        self.assertEqual((fields['token'], fields['device'], fields['sound']), ('testapp', 'iphone', 'siren'))
        self.assertLessEqual(len(fields['message']), 1024)
        self.assertLessEqual(len(fields['title']), 250)
        self.assertNotIn('url', fields)

    def test_pushover_error_classification(self):
        cfg = config()
        cases = [(http_error(400, {'token': 'invalid', 'errors': ['application token is invalid']}), 'config'),
                 (http_error(400, {'user': 'invalid', 'errors': ['user identifier is invalid']}), 'config'),
                 (http_error(400, {'errors': ['message cannot be blank']}), 'message'),
                 (http_error(429, {}), 'quota'), (http_error(503, {}), 'temporary'),
                 (urllib.error.URLError('dns'), 'temporary')]
        for error, kind in cases:
            with patch('urllib.request.urlopen', side_effect=error):
                with self.assertRaises(pa.PushoverError) as ctx:
                    pa.push(cfg, {'title': 't', 'message': 'm'})
            self.assertEqual(ctx.exception.kind, kind)
            self.assertNotIn('testapp', str(ctx.exception))

    def test_failure_tracker(self):
        cfg = config(FAIL_ALERT_THRESHOLD='2')
        notes = []
        with patch.object(pa, 'notify', lambda c, t, m, p=0: notes.append(t)):
            tracker = pa.FailureTracker(cfg)
            tracker.failed('X @a', 'e')
            tracker.failed('X @a', 'e')
            tracker.failed('X @a', 'e')
            tracker.ok('X @a')
        self.assertEqual(notes, ['postalarm: Quelle gestört', 'postalarm: Quelle wieder ok'])


class FeedHandler(BaseHTTPRequestHandler):
    responses = {}
    requests = []

    def do_GET(self):
        FeedHandler.requests.append(self.path)
        account = self.path.split('/')[3]
        code, body = FeedHandler.responses.get(account, (404, '<code class="x details">nope</code>'))
        self.send_response(code)
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *_):
        pass


class XTests(Base):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(('127.0.0.1', 0), FeedHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def feed(self, account, ids):
        items = [{'id': f'https://x.com/{account}/status/{i}', 'url': f'https://x.com/{account}/status/{i}',
                  'content_html': f'Tweet <b>{i}</b><br>Zeile'} for i in ids]
        FeedHandler.responses[account] = (200, json.dumps({'version': 'https://jsonfeed.org/version/1.1',
                                                            'items': items}))

    def test_url(self):
        cfg = config(RSSHUB_URL='http://h:1200/', ACCESS_KEY='k&1', X_INCLUDE_REPLIES='true')
        self.assertEqual(pa.x_feed_url(cfg['x'], 'foo'),
                         'http://h:1200/twitter/user/foo/includeRts=false&includeReplies=true?format=json&key=k%261')

    def test_first_run_then_new_tweets_oldest_first(self):
        cfg = config(RSSHUB_URL=self.url, X_ACCOUNTS='acc')
        tracker = pa.FailureTracker(cfg)
        self.feed('acc', [3, 2, 1])
        self.assertTrue(pa.poll_x(self.db, cfg, tracker))
        self.assertEqual(self.db.execute('SELECT count(*) FROM pending').fetchone()[0], 0)
        self.feed('acc', [5, 4, 3, 2, 1])
        self.reopen()
        self.assertTrue(pa.poll_x(self.db, cfg, tracker))
        rows = self.db.execute('SELECT id, payload FROM pending ORDER BY rowid').fetchall()
        self.assertEqual([r[0] for r in rows], ['x:acc:https://x.com/acc/status/4', 'x:acc:https://x.com/acc/status/5'])
        payload = json.loads(rows[0][1])
        self.assertEqual(payload['message'], 'Tweet 4\nZeile')
        self.assertEqual(payload['url'], 'https://x.com/acc/status/4')
        self.assertIn('format=json', FeedHandler.requests[-1])
        # Gleicher Feed erneut: keine Duplikate.
        self.assertTrue(pa.poll_x(self.db, cfg, tracker))
        self.assertEqual(self.db.execute('SELECT count(*) FROM pending').fetchone()[0], 2)

    def test_error_and_selfheal(self):
        flag = Path(self.tmp.name) / 'restart'
        cfg = config(RSSHUB_URL=self.url, X_ACCOUNTS='broken', SELFHEAL_AFTER='2', SELFHEAL_MAX_PER_DAY='1',
                     RESTART_FLAG=str(flag), FAIL_ALERT_THRESHOLD='100')
        FeedHandler.responses['broken'] = (503, '<code class="x details">Twitter API error: 404</code>')
        tracker = pa.FailureTracker(cfg)
        with patch.object(pa, 'notify', lambda *a, **k: None):
            self.assertFalse(pa.poll_x(self.db, cfg, tracker))
            self.assertFalse(flag.exists())
            self.assertFalse(pa.poll_x(self.db, cfg, tracker))
            self.assertTrue(flag.exists())
            flag.unlink()
            pa.poll_x(self.db, cfg, tracker)
            pa.poll_x(self.db, cfg, tracker)
        self.assertFalse(flag.exists(), 'Tageslimit muss greifen')

    def test_unreachable(self):
        cfg = config(RSSHUB_URL='http://127.0.0.1:9', X_ACCOUNTS='acc', X_REQUEST_TIMEOUT='5')
        with self.assertRaises(RuntimeError):
            pa.fetch_x(cfg['x'], 'acc')


class DiscordTests(Base):
    def test_filters(self):
        for users, channels, guilds in [({'99'}, set(), set()), ({'30'}, {'99'}, set()), ({'30'}, set(), {'99'})]:
            self.assertEqual(pa.ingest_discord(self.db, DOC, users, channels, guilds), 0)
        self.assertEqual(pa.ingest_discord(self.db, DOC, {'30'}, {'20'}, {'10'}), 1)

    def test_dedup_and_link(self):
        cfg = config()
        pa.ingest_discord(self.db, DOC, {'30'}, set(), set())
        pa.ingest_discord(self.db, DOC, {'30'}, set(), set())
        sent = []
        pa.deliver(self.db, cfg, lambda c, f: sent.append(f))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]['url'], 'https://discord.com/channels/10/20/100')
        self.assertEqual(sent[0]['title'], 'Alice in #general')
        self.reopen()
        self.assertEqual(pa.ingest_discord(self.db, DOC, {'30'}, set(), set()), 0)

    def test_prune_never_passes_a_cursor(self):
        old = pa.snowflake_at(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=40))
        new = pa.snowflake_at(dt.datetime.now(dt.timezone.utc))
        with self.db:
            self.db.executemany('INSERT INTO sent VALUES (?,?,?)',
                                [(f'discord:{old - 10}', 'discord', 0), (f'discord:{old}', 'discord', 0),
                                 (f'discord:{new}', 'discord', 0), ('x:a:1', 'x', 0), ('x:a:2', 'x', int(time.time()))])
            self.db.execute('INSERT INTO cursors VALUES (?,?)', ('20', str(old - 10)))
        self.assertEqual(pa.prune(self.db, 30), 2)
        left = {r[0] for r in self.db.execute('SELECT id FROM sent')}
        self.assertEqual(left, {f'discord:{old}', f'discord:{new}', 'x:a:2'})

    def test_export_passes_only_discord_token(self):
        script = ('import json, os, sys\na = sys.argv\n'
                  'assert "--token" not in a and "-t" not in a\n'
                  'assert os.environ["DISCORD_TOKEN"] == "discord-secret-token"\n'
                  'assert "PUSHOVER_TOKEN" not in os.environ and "TWITTER_AUTH_TOKEN" not in os.environ\n'
                  f'json.dump({DOC!r}, open(a[a.index("-o") + 1], "w"))\n')
        work = Path(self.tmp.name) / 'w'
        work.mkdir()
        with patch.dict(os.environ, {**ENV, 'TWITTER_AUTH_TOKEN': 'xsecret',
                                     'DCE_EXECUTABLE': fake_dce(self.tmp.name, script)}):
            self.assertEqual(pa.export('20', '100', work), DOC)

    def test_export_error_is_redacted(self):
        script = 'import sys\nsys.stderr.write("Token discord-secret-token is invalid\\n")\nsys.exit(2)\n'
        work = Path(self.tmp.name) / 'w'
        work.mkdir()
        with patch.dict(os.environ, {**ENV, 'DCE_EXECUTABLE': fake_dce(self.tmp.name, script)}):
            with self.assertRaises(RuntimeError) as ctx:
                pa.export('20', '100', work)
        self.assertIn('exit=2', str(ctx.exception))
        self.assertNotIn('discord-secret-token', str(ctx.exception))

    def test_export_timeout_kills_process(self):
        with patch.dict(os.environ, {'DCE_EXECUTABLE': fake_dce(self.tmp.name, 'import time\ntime.sleep(30)\n')}):
            start = time.monotonic()
            with self.assertRaises(TimeoutError):
                pa.run_dce(['export'], 1, Path(self.tmp.name) / 'err.log')
            self.assertLess(time.monotonic() - start, 10)


ARCHIVE_DCE = (
    'import json, os, sys\n'
    'a = sys.argv\n'
    'log = open(ARGS_LOG, "a"); log.write(json.dumps(a[1:]) + "\\n"); log.close()\n'
    'assert "ARGS_LOG" not in os.environ and "PUSHOVER_TOKEN" not in os.environ\n'
    'mode = open(MODE_FILE).read().strip()\n'
    'out = a[a.index("-o") + 1].replace("%G", "Server").replace("%C", "kanal")\n'
    'os.makedirs(os.path.dirname(out), exist_ok=True)\n'
    'open(out, "w").write("<html>chat</html>")\n'
    'if mode == "empty":\n'
    '    sys.stderr.write("Channel kanal of guild Server does not contain any messages within the specified period; an empty file will be created.\\n")\n'
    'if mode == "fail":\n'
    '    sys.stderr.write("Authentication token discord-secret-token is invalid.\\n"); sys.exit(1)\n'
)


class ArchiveTests(Base):
    def setUp(self):
        super().setUp()
        self.dir = Path(self.tmp.name) / 'archive'
        self.mode = Path(self.tmp.name) / 'mode'
        self.argslog = Path(self.tmp.name) / 'args.log'
        script = f'ARGS_LOG = {str(self.argslog)!r}\nMODE_FILE = {str(self.mode)!r}\n' + ARCHIVE_DCE
        self.env = {'DCE_EXECUTABLE': fake_dce(self.tmp.name, script), 'ARGS_LOG': 'leak-check'}

    def cfg(self, **kw):
        return config(DISCORD_ARCHIVE='true', DISCORD_ARCHIVE_DIR=str(self.dir), **kw)

    def run_archive(self, cfg, mode, minutes=0):
        self.mode.write_text(mode)
        now = dt.datetime(2026, 10, 27, 12, 0, tzinfo=dt.timezone.utc) + dt.timedelta(minutes=minutes)
        with patch.dict(os.environ, {**ENV, **self.env}):
            return pa.archive_channel(self.db, cfg, '20', now)

    def calls(self):
        return [json.loads(line) for line in self.argslog.read_text().splitlines()]

    def test_config(self):
        cfg = self.cfg()
        self.assertEqual(cfg['archive']['channels'], {'20'})
        self.assertEqual(cfg['archive']['format'], ('HtmlDark', 'html'))
        self.assertEqual(cfg['archive']['interval'], 86400)
        cfg = self.cfg(DISCORD_ARCHIVE_CHANNEL_IDS='7,8', DISCORD_ARCHIVE_FORMAT='PlainText',
                       DISCORD_ARCHIVE_SINCE='2026-01-01', DISCORD_ARCHIVE_PARTITION='20MB')
        self.assertEqual(cfg['archive']['channels'], {'7', '8'})
        self.assertEqual(cfg['archive']['format'], ('PlainText', 'txt'))
        self.assertEqual(cfg['archive']['partition'], '20mb')
        self.assertEqual(int(cfg['archive']['since']),
                         pa.snowflake_at(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)))
        self.assertIsNone(config()['archive'])
        # Archiv allein, ohne Überwachung und ohne X
        only = config(X_ACCOUNTS='', DISCORD_CHANNEL_IDS='', DISCORD_USER_IDS='', DISCORD_ARCHIVE='true',
                      DISCORD_ARCHIVE_CHANNEL_IDS='9')
        self.assertEqual(only['archive']['channels'], {'9'})
        for key, value in [('DISCORD_ARCHIVE_FORMAT', 'pdf'), ('DISCORD_ARCHIVE_SINCE', 'gestern'),
                           ('DISCORD_ARCHIVE_PARTITION', 'viel'), ('DISCORD_ARCHIVE_INTERVAL_HOURS', '0')]:
            with self.assertRaises(pa.ConfigError, msg=key):
                self.cfg(**{key: value})
        with self.assertRaises(pa.ConfigError):
            config(DISCORD_CHANNEL_IDS='', DISCORD_USER_IDS='', DISCORD_ARCHIVE='true')

    def test_full_then_incremental(self):
        cfg = self.cfg(DISCORD_ARCHIVE_MEDIA='true')
        self.assertEqual(self.run_archive(cfg, 'ok'), 1)
        first = self.calls()[0]
        self.assertNotIn('--after', first, 'erster Lauf exportiert die ganze Historie')
        self.assertEqual(first[first.index('-f') + 1], 'HtmlDark')
        self.assertIn('--media', first)
        self.assertTrue(first[first.index('--media-dir') + 1].endswith('/'))
        self.assertNotIn('--token', first)
        files = list(self.dir.glob('Server/kanal/*.html'))
        self.assertEqual(len(files), 1)
        # Zweiter Lauf schließt exakt an das "before" des ersten an
        self.run_archive(cfg, 'ok', minutes=60)
        second = self.calls()[1]
        self.assertEqual(second[second.index('--after') + 1], first[first.index('--before') + 1])
        self.assertEqual(len(list(self.dir.glob('Server/kanal/*.html'))), 2)

    def test_empty_period_leaves_no_file_but_advances(self):
        cfg = self.cfg()
        self.assertEqual(self.run_archive(cfg, 'empty'), 0)
        self.assertEqual(list(self.dir.glob('**/*.html')), [])
        self.assertIsNotNone(pa.kv_get(self.db, 'archive:20'))

    def test_failure_removes_partial_file_and_keeps_cursor(self):
        cfg = self.cfg()
        with self.assertRaises(RuntimeError) as ctx:
            self.run_archive(cfg, 'fail')
        self.assertNotIn('discord-secret-token', str(ctx.exception))
        self.assertEqual(list(self.dir.glob('**/*.html')), [])
        self.assertIsNone(pa.kv_get(self.db, 'archive:20'))

    def test_since(self):
        cfg = self.cfg(DISCORD_ARCHIVE_SINCE='2026-10-01T00:00:00+00:00')
        self.run_archive(cfg, 'ok')
        args = self.calls()[0]
        self.assertEqual(args[args.index('--after') + 1], cfg['archive']['since'])

    def test_poll_reports_failures(self):
        cfg = self.cfg(FAIL_ALERT_THRESHOLD='100')
        self.mode.write_text('fail')
        with patch.dict(os.environ, {**ENV, **self.env}):
            self.assertFalse(pa.poll_archive(self.db, cfg, pa.FailureTracker(cfg)))
            self.mode.write_text('ok')
            self.assertTrue(pa.poll_archive(self.db, cfg, pa.FailureTracker(cfg)))


LIVE_DOC = {'guild': {'id': '10', 'name': 'Mein Server'}, 'channel': {'id': '20', 'name': 'gen/eral'},
            'messages': [
                {'id': '1002', 'timestamp': '2026-10-27T23:59:30+01:00', 'content': 'zweite\nZeile',
                 'author': {'id': '31', 'name': 'bob', 'nickname': 'Bob'},
                 'reference': {'messageId': '1001'}, 'timestampEdited': '2026-10-27T23:59:50+01:00',
                 'attachments': [{'fileName': 'a.png', 'url': 'https://cdn/a.png'}]},
                {'id': '1001', 'timestamp': '2026-10-27T10:00:00+01:00', 'content': 'erste',
                 'author': {'id': '30', 'name': 'alice'}},
                {'id': '1003', 'timestamp': '2026-10-28T00:00:10+01:00', 'content': '',
                 'author': {'id': '32', 'name': 'bot', 'isBot': True},
                 'embeds': [{'title': 'News', 'url': 'https://x'}]}]}


class LiveTests(Base):
    def cfg(self, **kw):
        return config(DISCORD_LIVE='true', DISCORD_ARCHIVE_DIR=str(Path(self.tmp.name) / 'archive'),
                      TZ='Europe/Berlin', **kw)

    def setUp(self):
        super().setUp()
        os.environ['TZ'] = 'Europe/Berlin'
        time.tzset()

    def test_config(self):
        self.assertTrue(self.cfg()['discord']['live'])
        self.assertFalse(config()['discord']['live'])
        with self.assertRaises(pa.ConfigError):
            config(DISCORD_CHANNEL_IDS='', DISCORD_USER_IDS='', DISCORD_LIVE='true')

    def test_append_by_day_sorted_and_deduplicated(self):
        cfg = self.cfg()
        self.assertTrue(pa.live_append(self.db, cfg, LIVE_DOC))
        folder = Path(self.tmp.name) / 'archive' / 'Mein Server' / 'gen_eral' / 'live'
        day1 = (folder / '2026-10-27.txt').read_text(encoding='utf-8')
        day2 = (folder / '2026-10-28.txt').read_text(encoding='utf-8')
        self.assertTrue(day1.startswith('# Mein Server / #gen/eral (Kanal 20)'))
        self.assertLess(day1.index('#1001'), day1.index('#1002'), 'chronologisch')
        self.assertIn('[2026-10-27 10:00:00] alice #1001\n  erste\n', day1)
        self.assertIn('[2026-10-27 23:59:30] Bob (@bob) #1002 ↪ #1001 (bearbeitet)\n  zweite\n  Zeile\n'
                      '  [Anhang] a.png https://cdn/a.png\n', day1)
        self.assertIn('[2026-10-28 00:00:10] bot #1003 [Bot]\n  [Link] News https://x\n', day2)
        # Gleicher Export erneut (z. B. nach Fehler): nichts doppelt, Kopfzeile nur einmal
        self.assertTrue(pa.live_append(self.db, cfg, LIVE_DOC))
        self.assertEqual((folder / '2026-10-27.txt').read_text(encoding='utf-8'), day1)
        newer = json.loads(json.dumps(LIVE_DOC))
        newer['messages'] = [{'id': '1004', 'timestamp': '2026-10-28T08:00:00+01:00', 'content': 'neu',
                              'author': {'id': '30', 'name': 'alice'}}]
        pa.live_append(self.db, cfg, newer)
        text = (folder / '2026-10-28.txt').read_text(encoding='utf-8')
        self.assertEqual(text.count('# Mein Server'), 1)
        self.assertTrue(text.endswith('[2026-10-28 08:00:00] alice #1004\n  neu\n'))

    def test_write_failure_keeps_monitor_cursor(self):
        cfg = self.cfg()
        blocker = Path(self.tmp.name) / 'archive'
        blocker.write_text('keine Ordner hier')  # mkdir schlägt fehl
        script = f'import json, sys\na = sys.argv\njson.dump({LIVE_DOC!r}, open(a[a.index("-o") + 1], "w"))\n'
        with self.db:
            self.db.execute('INSERT INTO cursors VALUES (?,?)', ('20', '1000'))
        with patch.dict(os.environ, {**ENV, 'DCE_EXECUTABLE': fake_dce(self.tmp.name, script)}):
            pa.poll_window(self.db, cfg, '20', '1000', None)
        self.assertEqual(self.db.execute("SELECT after FROM cursors WHERE channel='20'").fetchone()[0], '1000')
        self.assertIsNone(pa.kv_get(self.db, 'live:20'))
        # Alarm-Treffer (User 30) wurde trotzdem eingereiht
        self.assertEqual(self.db.execute('SELECT id FROM pending').fetchall(), [('discord:1001',)])
        blocker.unlink()
        with patch.dict(os.environ, {**ENV, 'DCE_EXECUTABLE': fake_dce(self.tmp.name, script)}):
            pa.poll_window(self.db, cfg, '20', '1000', None)
        self.assertEqual(self.db.execute("SELECT after FROM cursors WHERE channel='20'").fetchone()[0], '1003')
        self.assertEqual(self.db.execute('SELECT count(*) FROM pending').fetchone()[0], 1)


class AdaptivePollingTests(Base):
    def run_poll(self, cfg, doc):
        script = f'import json, sys\na = sys.argv\njson.dump({doc!r}, open(a[a.index("-o") + 1], "w"))\n'
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO cursors VALUES (?,?)', ('20', '1'))
        with patch.dict(os.environ, {**ENV, 'DCE_EXECUTABLE': fake_dce(self.tmp.name, script)}):
            return pa.poll_discord(self.db, cfg, pa.FailureTracker(cfg))

    def test_hourly_then_ten_minutes_after_hit(self):
        cfg = config(DISCORD_POLL_INTERVAL='3600', DISCORD_ACTIVE_INTERVAL='600', DISCORD_ACTIVE_DURATION='7200')
        self.assertEqual(pa.discord_interval(self.db, cfg), 3600)
        other = json.loads(json.dumps(DOC)); other['messages'][0]['author']['id'] = '99'
        self.assertTrue(self.run_poll(cfg, other))
        self.assertEqual(pa.discord_interval(self.db, cfg), 3600, 'fremder User: kein Wechsel')
        self.assertTrue(self.run_poll(cfg, DOC))
        self.assertEqual(pa.discord_interval(self.db, cfg), 600, 'überwachter User: 10 min')
        until = pa.kv_get(self.db, 'discord_active_until')
        self.assertAlmostEqual(until, time.time() + 7200, delta=5)
        # Übersteht Neustart (persistiert), läuft nach Ablauf zurück auf 1 h
        self.reopen()
        self.assertEqual(pa.discord_interval(self.db, cfg), 600)
        with patch('time.time', return_value=until + 1):
            self.assertEqual(pa.discord_interval(self.db, cfg), 3600)

    def test_disabled(self):
        cfg = config(DISCORD_POLL_INTERVAL='3600', DISCORD_ACTIVE_DURATION='0')
        self.run_poll(cfg, DOC)
        self.assertEqual(pa.discord_interval(self.db, cfg), 3600)
        cfg = config(DISCORD_POLL_INTERVAL='60', DISCORD_ACTIVE_INTERVAL='600')
        self.assertEqual(pa.discord_interval(self.db, cfg), 60, 'aktives Intervall nie länger als normales')

    def test_worker_uses_callable_interval(self):
        cfg = config(X_ACCOUNTS='', DATA_DIR=self.tmp.name, DISCORD_POLL_INTERVAL='3600')
        waits = []

        def fake_wait(seconds):
            waits.append(seconds)
            pa.STOP.set()  # Schleife nach dem ersten Durchlauf beenden
            return True
        try:
            with patch.object(pa.STOP, 'wait', fake_wait), patch.object(pa, 'deliver', lambda *a: True):
                pa.worker(cfg, 'discord', lambda db, c, tr: True, lambda db, c: 1234, 300)
        finally:
            pa.STOP.clear()
        self.assertEqual(waits, [1234])


class DceErrorTests(unittest.TestCase):
    def test_skips_stacktrace_and_redacts(self):
        stderr = ('Failed to export the following channel(s):\n'
                  'general: DiscordChatExporter.Core.Exceptions.DiscordChatExporterException: '
                  'Authentication token discord-secret-token is invalid.\n'
                  '   at DiscordChatExporter.Core.Discord.DiscordClient.GetJsonResponseAsync(String url)\n'
                  '   at CliFx.CommandLineApplication.RunAsync(IReadOnlyList`1 args) in /_/CliFx/CommandLineApplication.cs:164\n')
        with patch.dict(os.environ, {'DISCORD_TOKEN': 'discord-secret-token'}):
            msg = pa.dce_error(stderr)
        self.assertIn('Authentication token *** is invalid', msg)
        self.assertNotIn('CliFx', msg)
        self.assertEqual(pa.dce_error(''), 'keine Ausgabe')


class SwitchTests(unittest.TestCase):
    def test_disable_x_keeps_discord(self):
        cfg = config(X_ENABLED='false')
        self.assertIsNone(cfg['x'])
        self.assertIsNotNone(cfg['discord'])

    def test_disable_discord_disables_live_and_archive(self):
        cfg = config(DISCORD_ENABLED='false', DISCORD_LIVE='true', DISCORD_ARCHIVE='true')
        self.assertIsNone(cfg['discord'])
        self.assertIsNone(cfg['archive'])
        self.assertIsNotNone(cfg['x'])

    def test_all_off_is_not_an_error(self):
        cfg = config(X_ENABLED='false', DISCORD_ENABLED='OFF')
        self.assertIsNone(cfg['x'])
        self.assertIsNone(cfg['discord'])

    def test_invalid_switch(self):
        with self.assertRaises(pa.ConfigError):
            config(X_ENABLED='vielleicht')

    def test_start_sh_skips_rsshub_when_x_disabled(self):
        import subprocess
        script = Path(pa.__file__).with_name('start.sh').read_text()
        block = script[script.index('# X aus oder keine X-Accounts'):script.index('  exec $APP')]
        for value, expected in [('false', 'aus'), ('OFF', 'aus'), ('true', 'an'), ('', 'an')]:
            out = subprocess.run(['sh', '-c', block.replace('  echo "[start] X deaktiviert', '  echo aus; exit; echo "') +
                                  '\nfi\necho an'],
                                 env={'X_ENABLED': value, 'X_ACCOUNTS': 'a', 'PATH': os.environ['PATH']},
                                 capture_output=True, text=True).stdout.strip()
            self.assertEqual(out, expected, value)


class CatchupTests(Base):
    def test_backlog_is_exported_in_windows(self):
        log = Path(self.tmp.name) / 'calls.log'
        script = (f'import json, sys\na = sys.argv\nopen({str(log)!r}, "a").write(json.dumps(a[1:]) + "\\n")\n'
                  'json.dump({"guild": {"id": "10"}, "channel": {"id": "20", "name": "g"}, "messages": []}, '
                  'open(a[a.index("-o") + 1], "w"))\n')
        cfg = config(DISCORD_CATCHUP_HOURS='2')
        now = dt.datetime(2026, 10, 28, 10, 0, tzinfo=dt.timezone.utc)
        start = now - dt.timedelta(hours=9)
        with self.db:
            self.db.execute('INSERT INTO cursors VALUES (?,?)', ('20', str(pa.snowflake_at(start))))
        with patch.dict(os.environ, {**ENV, 'DCE_EXECUTABLE': fake_dce(self.tmp.name, script)}):
            pa.poll_channel(self.db, cfg, '20', now=now)
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        # 9 h Rückstand in 2-h-Fenstern: 4 begrenzte Exporte, dann einer bis jetzt
        self.assertEqual(len(calls), 5)
        afters = [c[c.index('--after') + 1] for c in calls]
        befores = [c[c.index('--before') + 1] for c in calls]
        for i in range(4):
            self.assertEqual(int(afters[i + 1]), int(befores[i]) - 1, 'lückenlos aneinander')
        self.assertAlmostEqual(pa.cursor_time(befores[3]).timestamp(),
                               (start + dt.timedelta(hours=8)).timestamp(), delta=1)
        self.assertIn('T', befores[4], 'letzter Export bis jetzt (ISO-Zeit)')

    def test_failed_window_keeps_cursor(self):
        cfg = config(DISCORD_CATCHUP_HOURS='2', EXPORT_TIMEOUT_SECONDS='30')
        start = dt.datetime(2026, 10, 28, 0, 0, tzinfo=dt.timezone.utc)
        with self.db:
            self.db.execute('INSERT INTO cursors VALUES (?,?)', ('20', str(pa.snowflake_at(start))))
        with patch.dict(os.environ, {**ENV, 'DCE_EXECUTABLE': fake_dce(self.tmp.name, 'import sys\nsys.exit(1)\n')}):
            with self.assertRaises(RuntimeError):
                pa.poll_channel(self.db, cfg, '20', now=start + dt.timedelta(hours=9))
        self.assertEqual(self.db.execute("SELECT after FROM cursors").fetchone()[0], str(pa.snowflake_at(start)))


class AuthInvalidTests(Base):
    def test_invalid_token_pauses_discord_and_notifies_once(self):
        script = ('import sys\nopen(sys.argv[0] + ".calls", "a").write("x")\n'
                  'sys.stderr.write("DiscordChatExporterException: Authentication token is invalid.\\n")\nsys.exit(1)\n')
        exe = fake_dce(self.tmp.name, script)
        cfg = config(DISCORD_CHANNEL_IDS='20,21')
        with self.db:
            for ch in ('20', '21'):
                self.db.execute('INSERT INTO cursors VALUES (?,?)', (ch, str(pa.snowflake_at(
                    dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)))))
        notes = []
        with patch.dict(os.environ, {**ENV, 'DCE_EXECUTABLE': exe}), \
                patch.object(pa, 'notify', lambda c, title, msg, prio=0: notes.append(title)):
            tracker = pa.FailureTracker(cfg)
            self.assertTrue(pa.poll_discord(self.db, cfg, tracker))
            self.assertTrue(pa.poll_discord(self.db, cfg, tracker))
        self.assertEqual(Path(exe + '.calls').read_text(), 'x', 'nur ein Exportversuch insgesamt')
        self.assertEqual(notes, ['postalarm: Discord-Token ungültig'])


class HealthTests(Base):
    def test_healthcheck(self):
        data = Path(self.tmp.name)
        with patch.dict(os.environ, {'DATA_DIR': str(data)}):
            self.assertEqual(pa.healthcheck(), 1)
            pa.write_heartbeat(data, 'x', time.time() + 60)
            pa.write_heartbeat(data, 'discord', time.time() + 60)
            self.assertEqual(pa.healthcheck(), 0)
            pa.write_heartbeat(data, 'discord', time.time() - 1)
            self.assertEqual(pa.healthcheck(), 1)


if __name__ == '__main__':
    unittest.main()
