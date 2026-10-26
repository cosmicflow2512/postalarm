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
