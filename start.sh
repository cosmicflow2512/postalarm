#!/bin/sh
# postalarm-Supervisor.
# Phase 1 (root): /data für PUID:PGID übernehmen, dann ohne root neu starten.
# Phase 2: RSSHub (nur wenn X-Accounts gesetzt) und postalarm starten und überwachen.
#          postalarm kann per Flag-Datei einen Neustart nur von RSSHub anfordern (Selbstheilung).
#          Stirbt ein Prozess unerwartet, beendet sich der Container (Restart-Policy greift).
set -u

PUID="${PUID:-99}"
PGID="${PGID:-100}"
DATA_DIR="${DATA_DIR:-${STATE_DIR:-/data}}"
export DATA_DIR

case "$PUID:$PGID" in
  *[!0-9:]*|:*|*:|*:*:*) echo "[start] PUID/PGID müssen numerisch sein" >&2; exit 1 ;;
esac

if [ "$(id -u)" = "0" ] && [ "$PUID" != "0" ]; then
  mkdir -p "$DATA_DIR"
  find "$DATA_DIR" \( ! -user "$PUID" -o ! -group "$PGID" \) -exec chown "$PUID:$PGID" {} +
  chmod 0700 "$DATA_DIR"
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$0" "$@"
fi

export HOME=/tmp NPM_CONFIG_CACHE=/tmp/.npm NPM_CONFIG_UPDATE_NOTIFIER=false
APP="python3 /opt/postalarm/postalarm.py"

# Nur Discord konfiguriert: kein RSSHub nötig.
if [ -z "${X_ACCOUNTS:-}${ACCOUNTS:-}" ]; then
  echo "[start] keine X-Accounts gesetzt, RSSHub bleibt aus"
  unset TWITTER_AUTH_TOKEN
  exec $APP
fi

# Ohne eigenen ACCESS_KEY einen zufälligen setzen: RSSHub mit X-Cookie ist dann
# auch für andere Container im selben Docker-Netz nicht ohne Schlüssel abrufbar.
if [ -z "${ACCESS_KEY:-}${RSSHUB_ACCESS_KEY:-}" ]; then
  ACCESS_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
fi
ACCESS_KEY="${ACCESS_KEY:-$RSSHUB_ACCESS_KEY}"
export ACCESS_KEY

FLAG="${RESTART_FLAG:-/tmp/postalarm-restart-rsshub}"
export RESTART_FLAG="$FLAG"
HEALTH_URL="http://127.0.0.1:${PORT:-1200}/healthz?key=${ACCESS_KEY}"
RSSHUB_PID=""
APP_PID=""

start_rsshub() {
  cd /app || return 1
  # RSSHub bekommt weder Pushover- noch Discord-Zugangsdaten.
  setsid env -u DISCORD_TOKEN -u PUSHOVER_TOKEN -u PUSHOVER_USER \
    -u PUSHOVER_API_TOKEN -u PUSHOVER_USER_KEY npm run start &
  RSSHUB_PID=$!
  echo "[start] warte auf RSSHub ..."
  i=0
  until curl -fs "$HEALTH_URL" >/dev/null 2>&1; do
    if ! kill -0 "$RSSHUB_PID" 2>/dev/null; then
      echo "[start] RSSHub ist beim Start abgestürzt"
      return 1
    fi
    i=$((i + 1))
    if [ "$i" -ge 120 ]; then
      echo "[start] RSSHub nach 120 s nicht erreichbar"
      return 1
    fi
    sleep 1
  done
  echo "[start] RSSHub läuft"
}

stop_rsshub() {
  [ -n "$RSSHUB_PID" ] || return 0
  kill -TERM "-$RSSHUB_PID" 2>/dev/null
  j=0
  while kill -0 "-$RSSHUB_PID" 2>/dev/null && [ "$j" -lt 15 ]; do sleep 1; j=$((j + 1)); done
  kill -KILL "-$RSSHUB_PID" 2>/dev/null
  wait "$RSSHUB_PID" 2>/dev/null
  RSSHUB_PID=""
}

stop_all() {
  if [ -n "$APP_PID" ]; then
    kill -TERM "$APP_PID" 2>/dev/null
    wait "$APP_PID" 2>/dev/null
  fi
  stop_rsshub
}

trap 'stop_all; exit 0' TERM INT

rm -f "$FLAG"
start_rsshub || { stop_all; exit 1; }

echo "[start] starte postalarm"
env -u TWITTER_AUTH_TOKEN $APP &
APP_PID=$!

while :; do
  if [ -f "$FLAG" ]; then
    rm -f "$FLAG"
    echo "[start] Selbstheilung: starte RSSHub neu"
    stop_rsshub
    start_rsshub || { stop_all; exit 1; }
  fi
  if ! kill -0 "$APP_PID" 2>/dev/null; then
    wait "$APP_PID"; code=$?
    echo "[start] postalarm beendet (exit $code), stoppe Container"
    stop_rsshub
    exit "$code"
  fi
  if ! kill -0 "$RSSHUB_PID" 2>/dev/null; then
    echo "[start] RSSHub wurde beendet, stoppe Container"
    stop_all
    exit 1
  fi
  sleep 2
done
