# postalarm

Ein Container für Unraid, der per Pushover alarmiert, sobald

- ein bestimmter **X/Twitter-Account** postet (eingebautes [RSSHub](https://docs.rsshub.app)), oder
- ein bestimmter **Discord-User** in ausgewählten Kanälen schreibt ([DiscordChatExporter](https://github.com/Tyrrrz/DiscordChatExporter)-CLI).

Beide Quellen sind optional, mindestens eine muss gesetzt sein. Der Alarm-Modus ist je Quelle einstellbar. Keine Weboberfläche, keine offenen Ports.

Nachfolger von `tweetalarm` und `discord-author-watcher`.

## Wichtige Einschränkungen

- **X:** Die RSSHub-Twitter-Route nutzt das Web-API eines echten Kontos (`auth_token`-Cookie). Das verstößt gegen die X-Nutzungsbedingungen, das Konto kann gesperrt werden. Einen Zweitaccount verwenden. Häufiges Polling erhöht das Risiko.
- **Discord:** Der Zugriff erfolgt mit einem **Discord-Benutzer-Token**. Automatisierte Benutzerkonten verstoßen laut [DiscordChatExporter-Dokumentation](https://github.com/Tyrrrz/DiscordChatExporter/blob/prime/.docs/Token-and-IDs.md) gegen Discords Nutzungsbedingungen, Sperrungen sind möglich. Es werden nur Nachrichten erfasst, auf die das Konto Zugriff hat.
- Beides ist Polling, nicht Echtzeit. Bei X kommt der RSSHub-Cache (`CACHE_EXPIRE`) zur Abfragezeit dazu. Bearbeitete oder gelöschte Discord-Nachrichten lösen keinen neuen Alarm aus. Threads müssen mit eigener ID eingetragen werden.

## Alarm-Modi

`X_MODE` und `DISCORD_MODE` getrennt (Standard: X `emergency`, Discord `normal`):

| Modus | Verhalten |
| --- | --- |
| `emergency` | Pushover Priority 2, wiederholt alle `EMERGENCY_RETRY` s bis zur Bestätigung oder bis `EMERGENCY_EXPIRE` |
| `burst` | `BURST_COUNT` Einzelnachrichten im Abstand `BURST_DELAY`, Priority `BURST_PRIORITY` |
| `high` | eine Nachricht, Priority 1 (umgeht Ruhezeiten in Pushover) |
| `normal` | eine Nachricht, Priority 0 |

Pushover-Grenzen laut [API-Doku](https://pushover.net/api): Retry mindestens 30 s, Expire höchstens 10800 s. Für iPhone in der Pushover-App kritische Hinweise erlauben, damit Emergency-Alarme Stummschalter und Fokus umgehen.

Zusätzlich gibt es Statusmeldungen: Wenn eine Quelle (ein X-Account oder ein Discord-Kanal) `FAIL_ALERT_THRESHOLD`-mal in Folge fehlschlägt, kommt eine Störungsmeldung mit Priority 1, nach Erholung eine Entwarnung.

## Unraid

```sh
mkdir -p /mnt/cache/appdata/postalarm /boot/config/plugins/dockerMan/templates-user
curl --fail --location \
  https://raw.githubusercontent.com/cosmicflow2512/postalarm/main/templates/my-postalarm.xml \
  -o /boot/config/plugins/dockerMan/templates-user/my-postalarm.xml
```

**Docker → Add Container → postalarm**, Pushover-Daten und mindestens eine Quelle eintragen, starten. Testalarm über den echten Alarmweg:

```sh
docker exec postalarm python3 /opt/postalarm/postalarm.py --test-alert x
docker exec postalarm python3 /opt/postalarm/postalarm.py --test-alert discord
```

### Umstieg von tweetalarm / discord-author-watcher

Werte automatisch übernehmen: Das Skript liest die Umgebungsvariablen der laufenden Container per `docker inspect` und trägt sie ins postalarm-Template ein. Ein vorhandenes Template wird vorher als `.bak` gesichert. Geheimnisse werden nicht ausgegeben, nur als übernommen markiert. Bei unterschiedlichen Pushover-Daten gewinnt tweetalarm, der Unterschied wird gemeldet. `TEST_ON_START` wird nicht übernommen.

```sh
docker inspect tweetalarm discord-author-watcher | \
  docker run --rm -i --entrypoint python3 \
    -v /boot/config/plugins/dockerMan/templates-user:/templates \
    ghcr.io/cosmicflow2512/postalarm:latest /opt/postalarm/migrate.py /templates/my-postalarm.xml
```

Danach in Unraid **Add Container → postalarm** wählen, bei einem bestehenden Container **Edit**. Die Felder sind dann vorausgefüllt.

Alte Variablennamen funktionieren weiter: `ACCOUNTS`, `MODE`, `POLL_INTERVAL`, `INCLUDE_RETWEETS`, `INCLUDE_REPLIES`, `PUSHOVER_API_TOKEN`, `PUSHOVER_USER_KEY`, `POLL_INTERVAL_SECONDS`, `STATE_DIR`. Neue Namen haben Vorrang.

Der Zustand wird nicht übernommen, das ist auch nicht nötig. X markiert beim ersten Lauf alle vorhandenen Tweets als gesehen. Discord beginnt ab Start, optional mit `INITIAL_LOOKBACK_SECONDS`. Die alten Container erst stoppen, wenn postalarm läuft, sonst gibt es eine Lücke. Wenn beide parallel laufen, kommen Alarme doppelt.

## Konfiguration

| Variable | Standard | Bedeutung |
| --- | --- | --- |
| `PUSHOVER_TOKEN` / `PUSHOVER_USER` | – | Pflicht |
| `PUSHOVER_DEVICE`, `PUSHOVER_SOUND` | – | optional |
| `X_ACCOUNTS` | – | X-Namen ohne @, kommasepariert; leer = X aus, kein RSSHub |
| `TWITTER_AUTH_TOKEN` | – | `auth_token`-Cookie, nur RSSHub bekommt ihn |
| `X_MODE` | `emergency` | siehe Alarm-Modi |
| `X_POLL_INTERVAL` | `60` | Sekunden, Minimum 30 |
| `X_INCLUDE_RETWEETS`, `X_INCLUDE_REPLIES` | `false` | RSSHub-Routenparameter |
| `CACHE_EXPIRE` | `60` | RSSHub-Route-Cache in Sekunden |
| `ACCESS_KEY` | zufällig | RSSHub-Zugangsschlüssel, leer = bei jedem Start neu erzeugt |
| `SELFHEAL_AFTER`, `SELFHEAL_MAX_PER_DAY` | `3`, `3` | RSSHub-Neustart nach Twitter-API-404 in Folge, mit Tageslimit |
| `DISCORD_CHANNEL_IDS` | – | Channel-/Thread-IDs; leer = Discord aus |
| `DISCORD_USER_IDS` | – | Pflicht, wenn Discord an; geprüft wird die Autor-ID |
| `DISCORD_TOKEN` | – | nur DiscordChatExporter bekommt ihn |
| `DISCORD_GUILD_IDS` | – | optionaler zusätzlicher Serverfilter |
| `DISCORD_MODE` | `normal` | siehe Alarm-Modi |
| `DISCORD_POLL_INTERVAL` | `60` | Sekunden, Minimum 30 |
| `INITIAL_LOOKBACK_SECONDS` | `0` | nur beim allerersten Start je Kanal |
| `EXPORT_TIMEOUT_SECONDS` | `300` | je Kanalexport, 30–3600 |
| `EMERGENCY_RETRY`, `EMERGENCY_EXPIRE` | `30`, `3600` | Emergency-Modus |
| `BURST_COUNT`, `BURST_DELAY`, `BURST_PRIORITY` | `10`, `2`, `1` | Burst-Modus |
| `FAIL_ALERT_THRESHOLD`, `FAIL_ALERT_REPEAT_MIN` | `5`, `60` | Störungsmeldungen |
| `SENT_RETENTION_DAYS` | `30` | Aufbewahrung zugestellter IDs |
| `TEST_ON_START` | `false` | Testalarm je aktiver Quelle beim Start |
| `PUID` / `PGID` | `99` / `100` | Benutzer im Container |
| `TZ`, `LOG_LEVEL` | `Europe/Berlin`, `INFO` | |

Ungültige Werte beenden den Start mit `Konfigurationsfehler: …` im Log.

IDs und Tokens: Discord-Entwicklermodus (Einstellungen → Erweitert), dann Rechtsklick → ID kopieren. Token-Anleitung siehe [Token-and-IDs.md](https://github.com/Tyrrrz/DiscordChatExporter/blob/prime/.docs/Token-and-IDs.md). X-Cookie: in einer eingeloggten x.com-Session in den Browser-DevTools unter Cookies den Wert `auth_token` kopieren.

## Sicherheitsmodell

- Der Supervisor startet als root, übernimmt `/data` für `PUID:PGID` (Rechte `0700`) und startet sich per `setpriv` ohne root neu. RSSHub (Node), postalarm (Python) und DiscordChatExporter laufen danach als dieser Benutzer. Auch der Init-Prozess (`dumb-init`) läuft ohne root, damit `docker stop` trotz entfernter Capabilities funktioniert. Kein `--init` hinzufügen: Ein Init unter root ohne `CAP_KILL` kann Prozessen von uid 99 keine Signale schicken.
- Template und Compose setzen `no-new-privileges`, entfernen alle Capabilities bis auf die für den Benutzerwechsel nötigen und begrenzen Speicher und Prozesse.
- Jede Komponente bekommt nur ihre eigenen Zugangsdaten: RSSHub den X-Cookie, DiscordChatExporter den Discord-Token (per Umgebung, nie als Argument), postalarm die Pushover-Daten.
- RSSHub ist immer per `ACCESS_KEY` geschützt und wird nicht nach außen veröffentlicht. Zum Debuggen kann ein Port 1200 ergänzt werden, Aufruf dann mit `?key=…`.
- Fehlerausgaben des Exporters werden gekürzt und der Token darin geschwärzt. Nachrichteninhalte stehen nicht im Log, Tweets nur als Link.

## Zustellung

SQLite in `/data/postalarm.sqlite3` mit einer gemeinsamen Warteschlange für beide Quellen. Treffer bleiben nach Fehlern und Neustarts erhalten. Burst-Serien setzen nach einem Abbruch an der richtigen Stelle fort.

| Pushover-Antwort | Verhalten |
| --- | --- |
| 4xx, ungültiges Token oder User | Warteschlange bleibt, Zustellung pausiert |
| 4xx wegen der Nachricht | nur dieser Treffer wird verworfen (`rejected`) |
| 429 Monatslimit, 5xx, Netzwerk | Warteschlange bleibt, Wiederholung mit Backoff bis 15 min |

Genau-einmal-Zustellung ist nicht garantiert. Geht die Antwort von Pushover verloren, kann es eine Wiederholung geben, weil Pushover keinen Idempotenzschlüssel anbietet.

## Healthcheck

Jede aktive Quelle schreibt nach jedem Durchlauf `/data/heartbeat-<quelle>` mit dem spätesten erwarteten nächsten Lebenszeichen. Der Docker-`HEALTHCHECK` meldet `unhealthy`, sobald eine Frist überschritten ist. Stirbt RSSHub oder die App, beendet sich der Container, und die Restart-Policy greift.

## Entwicklung

Python 3.11+, keine Pakete:

```sh
python3 -m unittest discover -s tests -v
docker build -t postalarm:local .
docker run --rm -e HOME=/tmp -v "$(mktemp -d):/data" --entrypoint python3 postalarm:local /opt/postalarm/postalarm.py --self-test
```

Die CI testet Python, baut ein amd64-Testimage und prüft darin: Tests mit dem Python im Image, Selbsttest von DiscordChatExporter als uid 99 mit entfernten Capabilities, Start von RSSHub und App ohne root, Trennung der Zugangsdaten, den RSSHub-Schlüssel und sauberes `docker stop`. Erst danach werden die amd64/arm64-Images veröffentlicht. Das Image wird wöchentlich neu gebaut, um RSSHub-Updates zu ziehen.

DiscordChatExporter `2.48` wird als offizielles Release-Asset geladen und per SHA-256 geprüft. Für ein Update `DCE_VERSION` und die beiden Prüfsummen im Dockerfile anpassen.

## Lizenz

MIT, siehe LICENSE. RSSHub und DiscordChatExporter sind eigenständige Projekte unter ihren eigenen Lizenzen.
