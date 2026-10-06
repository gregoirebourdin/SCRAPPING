# syntax=docker/dockerfile:1
# Railway service D — SearXNG (AGPL-3.0-or-later), run UNMODIFIED; we only ship a settings file.
# Pinned by tag AND digest (Docker Hub, 2026-10-04): bump deliberately, re-run tests/unit/search, then deploy.
FROM searxng/searxng:2026.10.4-d48c4b555@sha256:76b0bf285aca014c7191fc4d9234c4bfb358624ac33d8883833d496c059ec072

# Private-network metasearch backend for the Scout API (JSON API on, limiter off, curated engines).
# The secret key is NOT in this file: set SEARXNG_SECRET on the Railway service (SearXNG refuses to start
# with the placeholder value).
COPY --chown=977:977 settings.yml /etc/searxng/settings.yml
ENV SEARXNG_SETTINGS_PATH=/etc/searxng/settings.yml \
    GRANIAN_PORT=8080 \
    GRANIAN_HOST="::"

# Granian listens on [::]:8080 (IPv6 + IPv4): Railway private networking is IPv6.
EXPOSE 8080
# Upstream entrypoint/CMD are kept as is (/usr/local/searxng/entrypoint.sh → granian searx.webapp:app).
