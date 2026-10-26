"""Überträgt Werte aus tweetalarm und discord-author-watcher in das postalarm-Unraid-Template.

Eingabe (stdin): Ausgabe von `docker inspect <container> ...` (JSON-Liste).
Ausgabe: das Template (Standard /templates/my-postalarm.xml), vorher als .bak gesichert.
Geheimnisse werden nie ausgegeben, nur ob sie übernommen wurden.
"""
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

BUNDLED_TEMPLATE = Path(__file__).with_name('my-postalarm.xml')
SECRETS = {'PUSHOVER_TOKEN', 'PUSHOVER_USER', 'TWITTER_AUTH_TOKEN', 'DISCORD_TOKEN', 'ACCESS_KEY'}

# alter Name -> postalarm-Name, je Quelle
TWEETALARM = {
    'ACCOUNTS': 'X_ACCOUNTS', 'TWITTER_AUTH_TOKEN': 'TWITTER_AUTH_TOKEN', 'MODE': 'X_MODE',
    'POLL_INTERVAL': 'X_POLL_INTERVAL', 'INCLUDE_RETWEETS': 'X_INCLUDE_RETWEETS',
    'INCLUDE_REPLIES': 'X_INCLUDE_REPLIES', 'PUSHOVER_TOKEN': 'PUSHOVER_TOKEN',
    'PUSHOVER_USER': 'PUSHOVER_USER', 'PUSHOVER_DEVICE': 'PUSHOVER_DEVICE',
    'PUSHOVER_SOUND': 'PUSHOVER_SOUND', 'EMERGENCY_RETRY': 'EMERGENCY_RETRY',
    'EMERGENCY_EXPIRE': 'EMERGENCY_EXPIRE', 'BURST_COUNT': 'BURST_COUNT', 'BURST_DELAY': 'BURST_DELAY',
    'BURST_PRIORITY': 'BURST_PRIORITY', 'CACHE_EXPIRE': 'CACHE_EXPIRE', 'ACCESS_KEY': 'ACCESS_KEY',
    'SELFHEAL_AFTER': 'SELFHEAL_AFTER', 'SELFHEAL_MAX_PER_DAY': 'SELFHEAL_MAX_PER_DAY',
    'FAIL_ALERT_THRESHOLD': 'FAIL_ALERT_THRESHOLD', 'FAIL_ALERT_REPEAT_MIN': 'FAIL_ALERT_REPEAT_MIN',
    'LOG_LEVEL': 'LOG_LEVEL', 'TZ': 'TZ',
}
WATCHER = {
    'DISCORD_TOKEN': 'DISCORD_TOKEN', 'DISCORD_USER_IDS': 'DISCORD_USER_IDS',
    'DISCORD_CHANNEL_IDS': 'DISCORD_CHANNEL_IDS', 'DISCORD_GUILD_IDS': 'DISCORD_GUILD_IDS',
    'PUSHOVER_API_TOKEN': 'PUSHOVER_TOKEN', 'PUSHOVER_USER_KEY': 'PUSHOVER_USER',
    'POLL_INTERVAL_SECONDS': 'DISCORD_POLL_INTERVAL', 'INITIAL_LOOKBACK_SECONDS': 'INITIAL_LOOKBACK_SECONDS',
    'EXPORT_TIMEOUT_SECONDS': 'EXPORT_TIMEOUT_SECONDS', 'SENT_RETENTION_DAYS': 'SENT_RETENTION_DAYS',
    'TZ': 'TZ', 'PUID': 'PUID', 'PGID': 'PGID', 'LOG_LEVEL': 'LOG_LEVEL',
}


def kind(container):
    image = (container.get('Config') or {}).get('Image', '').lower()
    name = container.get('Name', '').lstrip('/').lower()
    if 'tweetalarm' in image or 'tweetalarm' in name:
        return 'tweetalarm'
    if 'discord-author-watcher' in image or 'discord-author-watcher' in name:
        return 'discord-author-watcher'
    return None


def env_of(container):
    pairs = (e.split('=', 1) for e in (container.get('Config') or {}).get('Env') or [] if '=' in e)
    return {k: v for k, v in pairs}


def normalize(key, value):
    value = value.strip()
    if key == 'BURST_DELAY':  # tweetalarm erlaubte Kommazahlen, postalarm ganze Sekunden
        try:
            return str(max(0, round(float(value))))
        except ValueError:
            return value
    if key in ('X_MODE',):
        return value.lower()
    return value


def collect(containers):
    values, origin, notes = {}, {}, []
    found = {}
    for container in containers:
        k = kind(container)
        if k:
            found[k] = container.get('Name', '').lstrip('/')
    # tweetalarm zuerst: bei Pushover-Konflikten gewinnt tweetalarm (Emergency-Alarme), Konflikt wird gemeldet
    order = sorted((c for c in containers if kind(c)), key=lambda c: kind(c) != 'tweetalarm')
    for container in order:
        k = kind(container)
        mapping = TWEETALARM if k == 'tweetalarm' else WATCHER
        for old, value in env_of(container).items():
            new = mapping.get(old)
            if not new or not value.strip():
                continue
            value = normalize(new, value)
            if new in values and values[new] != value:
                if new in SECRETS:
                    notes.append(f'{new}: Werte in {origin[new]} und {k} unterschiedlich, behalte {origin[new]}')
                elif new not in ('TZ', 'LOG_LEVEL'):
                    notes.append(f'{new}: {origin[new]}={values[new]!r}, {k}={value!r}; behalte {origin[new]}')
                continue
            values.setdefault(new, value)
            origin.setdefault(new, k)
    return values, origin, notes, found


def apply(template_path, values):
    tree = ET.parse(template_path)
    root = tree.getroot()
    targets = {c.get('Target'): c for c in root.iter('Config')}
    applied, skipped = [], []
    for key, value in values.items():
        node = targets.get(key)
        if node is None:
            skipped.append(key)
            continue
        node.text = value
        applied.append(key)
    return tree, applied, skipped


def main(argv):
    target = Path(argv[1] if len(argv) > 1 else '/templates/my-postalarm.xml')
    try:
        containers = json.load(sys.stdin)
    except ValueError:
        print('Keine gültige docker-inspect-Ausgabe auf stdin', file=sys.stderr)
        return 1
    values, origin, notes, found = collect(containers if isinstance(containers, list) else [])
    for name in ('tweetalarm', 'discord-author-watcher'):
        print(f'{name}: {"gefunden als " + found[name] if name in found else "NICHT gefunden"}')
    if not values:
        print('Keine Werte zum Übernehmen gefunden.', file=sys.stderr)
        return 1
    source = target if target.exists() else BUNDLED_TEMPLATE
    tree, applied, skipped = apply(source, values)
    if target.exists():
        backup = target.with_name(target.name + '.bak')
        backup.write_bytes(target.read_bytes())
        print(f'Sicherung: {backup}')
    tree.write(target, encoding='utf-8', xml_declaration=True)
    print(f'Geschrieben: {target}')
    for key in sorted(applied):
        shown = '(übernommen, verborgen)' if key in SECRETS else values[key]
        print(f'  {key} = {shown}   [{origin[key]}]')
    for key in skipped:
        print(f'  {key}: nicht im Template, übersprungen')
    for note in notes:
        print(f'  Hinweis: {note}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
