import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import migrate  # noqa: E402

TEMPLATE = Path(__file__).resolve().parents[1] / 'templates' / 'my-postalarm.xml'
INSPECT = [
    {'Name': '/tweetalarm', 'Config': {'Image': 'ghcr.io/cosmicflow2512/tweetalarm:latest', 'Env': [
        'ACCOUNTS=gezine_dev', 'TWITTER_AUTH_TOKEN=xcookie123', 'PUSHOVER_TOKEN=apptweet',
        'PUSHOVER_USER=userkey', 'MODE=Emergency', 'BURST_DELAY=2.5', 'POLL_INTERVAL=60',
        'TEST_ON_START=true', 'PUSHOVER_SOUND=', 'PATH=/usr/bin', 'NODE_VERSION=24', 'CACHE_EXPIRE=60']}},
    {'Name': '/discord-author-watcher', 'Config': {
        'Image': 'ghcr.io/cosmicflow2512/discord-author-watcher:latest', 'Env': [
            'DISCORD_TOKEN=dtoken456', 'DISCORD_USER_IDS=1,2', 'DISCORD_CHANNEL_IDS=1356976144418865384',
            'PUSHOVER_API_TOKEN=appdiscord', 'PUSHOVER_USER_KEY=userkey', 'POLL_INTERVAL_SECONDS=90',
            'PUID=99', 'PGID=100', 'TZ=Europe/Berlin']}},
    {'Name': '/jellyfin', 'Config': {'Image': 'jellyfin/jellyfin', 'Env': ['PUSHOVER_TOKEN=nope']}},
]


class MigrateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.target = Path(self.tmp.name) / 'my-postalarm.xml'

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, data):
        out = io.StringIO()
        with patch('sys.stdin', io.StringIO(json.dumps(data))), patch('sys.stdout', out):
            code = migrate.main(['migrate', str(self.target)])
        return code, out.getvalue()

    def values(self):
        root = ET.parse(self.target).getroot()
        return {c.get('Target'): (c.text or '') for c in root.iter('Config')}

    def test_full_migration_from_both(self):
        shutil.copy(TEMPLATE, self.target)
        code, out = self.run_main(INSPECT)
        self.assertEqual(code, 0)
        v = self.values()
        self.assertEqual(v['X_ACCOUNTS'], 'gezine_dev')
        self.assertEqual(v['TWITTER_AUTH_TOKEN'], 'xcookie123')
        self.assertEqual(v['X_MODE'], 'emergency')
        self.assertEqual(v['BURST_DELAY'], '2')
        self.assertEqual(v['DISCORD_TOKEN'], 'dtoken456')
        self.assertEqual(v['DISCORD_CHANNEL_IDS'], '1356976144418865384')
        self.assertEqual(v['DISCORD_POLL_INTERVAL'], '90')
        # Pushover-Konflikt: tweetalarm gewinnt, wird gemeldet
        self.assertEqual(v['PUSHOVER_TOKEN'], 'apptweet')
        self.assertIn('PUSHOVER_TOKEN: Werte in tweetalarm und discord-author-watcher unterschiedlich', out)
        # Testalarm beim Start wird bewusst nicht übernommen, Template-Standard bleibt
        self.assertEqual(v['TEST_ON_START'], 'false')
        # Fremde Container werden ignoriert, Geheimnisse nie ausgegeben
        for secret in ('xcookie123', 'dtoken456', 'apptweet', 'appdiscord', 'userkey', 'nope'):
            self.assertNotIn(secret, out)
        self.assertTrue((self.target.parent / 'my-postalarm.xml.bak').exists())
        self.assertTrue(self.target.read_text(encoding='utf-8').startswith("<?xml version='1.0' encoding='utf-8'?>"))

    def test_only_one_container_and_bundled_template(self):
        with patch.object(migrate, 'BUNDLED_TEMPLATE', TEMPLATE):
            code, out = self.run_main([INSPECT[1]])
        self.assertEqual(code, 0)
        self.assertIn('tweetalarm: NICHT gefunden', out)
        v = self.values()
        self.assertEqual(v['PUSHOVER_TOKEN'], 'appdiscord')
        self.assertEqual(v['X_ACCOUNTS'], '')

    def test_nothing_found(self):
        code, _ = self.run_main([INSPECT[2]])
        self.assertEqual(code, 1)
        self.assertFalse(self.target.exists())


if __name__ == '__main__':
    unittest.main()
