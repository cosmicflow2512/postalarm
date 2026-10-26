# RSSHub + DiscordChatExporter-CLI + postalarm in einem Container.
# Basis: offizielles RSSHub-Image (node:24-trixie-slim, WORKDIR /app, ENTRYPOINT dumb-init).
FROM diygod/rsshub:latest

ARG TARGETARCH
ARG DCE_VERSION=2.48
# SHA-256 der offiziellen Release-Assets DiscordChatExporter.Cli.linux-<arch>.zip (2.48)
ARG DCE_SHA256_AMD64=3e253e28ec7ea034b2201443fa84571142945299296541ecbe196ffceef8bc3c
ARG DCE_SHA256_ARM64=02a47fc8e0192fd509fbb082aadd9322035b18feae96849699fefc424a1e3379

# python3: postalarm (nur Standardbibliothek); libicu/libssl: .NET-Laufzeit der DCE-CLI;
# util-linux: setpriv für den Benutzerwechsel; curl: RSSHub-Healthcheck im Supervisor.
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 ca-certificates curl util-linux procps \
      libicu76 libssl3t64 \
 && rm -rf /var/lib/apt/lists/*

RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) rid=x64; sum="$DCE_SHA256_AMD64" ;; \
      arm64) rid=arm64; sum="$DCE_SHA256_ARM64" ;; \
      *) echo "Architektur $TARGETARCH nicht unterstützt" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/dce.zip \
      "https://github.com/Tyrrrz/DiscordChatExporter/releases/download/${DCE_VERSION}/DiscordChatExporter.Cli.linux-${rid}.zip"; \
    echo "$sum  /tmp/dce.zip" | sha256sum -c -; \
    python3 -c "import zipfile; zipfile.ZipFile('/tmp/dce.zip').extractall('/opt/dce')"; \
    rm /tmp/dce.zip; \
    chmod -R a+rX /opt/dce; \
    chmod 0755 /opt/dce/DiscordChatExporter.Cli

COPY postalarm.py migrate.py start.sh templates/my-postalarm.xml /opt/postalarm/
RUN chmod 0755 /opt/postalarm /opt/postalarm/start.sh \
 && chmod 0644 /opt/postalarm/postalarm.py /opt/postalarm/migrate.py /opt/postalarm/my-postalarm.xml

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data TZ=Europe/Berlin PUID=99 PGID=100 LANG=C.UTF-8 \
    DCE_EXECUTABLE=/opt/dce/DiscordChatExporter.Cli \
    RSSHUB_URL=http://127.0.0.1:1200 CACHE_EXPIRE=60 REQUEST_TIMEOUT=15000

VOLUME /data
HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=3 \
  CMD ["python3", "/opt/postalarm/postalarm.py", "--healthcheck"]

# start.sh startet dumb-init selbst, nachdem es auf PUID:PGID gewechselt hat.
ENTRYPOINT ["/opt/postalarm/start.sh"]
