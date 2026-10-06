# syntax=docker/dockerfile:1
# Railway service D — SearXNG (AGPL-3.0-or-later), run UNMODIFIED; we only ship a settings file.
# Pinned by tag AND digest (Docker Hub, 2026-10-04): bump deliberately, re-run tests/unit/search, then deploy.
FROM searxng/searxng:2026.10.4-d48c4b555@sha256:76b0bf285aca014c7191fc4d9234c4bfb358624ac33d8883833d496c059ec072

# Private-network metasearch backend for the Scout API (JSON API on, limiter off, curated engines).
# The secret key is NOT in this file: set SEARXNG_SECRET on the Railway service (SearXNG refuses to start
# with the placeholder value).
COPY --chown=977:977 settings.yml /etc/searxng/settings.yml
ENV SEARXNG_SETTINGS_PATH=/etc/searxng/settings.yml \
    GRANIAN_PORT=8080

# Granian listens on [::]:8080 (IPv6 + IPv4) — Railway private networking is IPv6 — and falls back to 0.0.0.0
# where IPv6 sockets are unavailable (local Docker without IPv6); SEARXNG_BIND_HOST forces a value. The upstream
# entrypoint then runs unchanged.
EXPOSE 8080
ENTRYPOINT ["/bin/sh", "-c", "if [ -n \"${SEARXNG_BIND_HOST:-}\" ]; then GRANIAN_HOST=\"$SEARXNG_BIND_HOST\"; elif python3 -c 'import socket; socket.socket(socket.AF_INET6).bind((\"::\", 0))' 2>/dev/null; then GRANIAN_HOST='::'; else GRANIAN_HOST=0.0.0.0; fi; export GRANIAN_HOST; exec /usr/local/searxng/entrypoint.sh \"$@\"", "--"]
