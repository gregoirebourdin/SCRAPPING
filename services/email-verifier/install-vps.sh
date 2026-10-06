#!/usr/bin/env bash
# Research — email verifier on your own server (Debian 12 / Ubuntu), behind HTTPS (Caddy, automatic certificate).
#
# Run as root, with the hostname whose DNS A record points at this server:
#   curl -fsSL https://raw.githubusercontent.com/gregoirebourdin/SCRAPPING/ccr-766d16c0-lecx31/services/email-verifier/install-vps.sh | bash -s -- verify.example.com
#
# Re-running it updates the verifier and keeps the same token. It never sends email: SMTP stops at RCPT TO.
set -euo pipefail

HOST="${1:?usage: install-vps.sh verify.example.com}"
BRANCH="${BRANCH:-ccr-766d16c0-lecx31}"
REPO="${REPO:-gregoirebourdin/SCRAPPING}"
DIR=/opt/research-verifier
ENV_FILE=/etc/research-verifier.env

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

say "Packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl ca-certificates openssl dnsutils >/dev/null

if ! command -v docker >/dev/null 2>&1; then
  say "Docker"
  curl -fsSL https://get.docker.com | sh >/dev/null
fi
systemctl enable --now docker >/dev/null 2>&1 || true

say "Verifier sources ($REPO@$BRANCH)"
rm -rf "$DIR/src" && mkdir -p "$DIR/src"
curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH" |
  tar -xz -C "$DIR/src" --strip-components=3 --wildcards "*/services/email-verifier/*"

if [ ! -f "$ENV_FILE" ]; then
  say "Secret token"
  umask 077
  cat >"$ENV_FILE" <<EOF
VERIFIER_TOKEN=$(openssl rand -hex 32)
SMTP_ENABLED=true
SMTP_HELO_DOMAIN=$HOST
SMTP_FROM=verify@$HOST
MAX_CONCURRENCY=8
EOF
fi

say "Build"
docker build -q -t research-verifier "$DIR/src" >/dev/null
docker network create research >/dev/null 2>&1 || true
docker rm -f research-verifier >/dev/null 2>&1 || true
docker run -d --name research-verifier --restart unless-stopped --network research \
  --env-file "$ENV_FILE" research-verifier >/dev/null

say "HTTPS for $HOST"
mkdir -p "$DIR/caddy"
cat >"$DIR/caddy/Caddyfile" <<EOF
$HOST {
  reverse_proxy research-verifier:8080
}
EOF
docker rm -f research-caddy >/dev/null 2>&1 || true
docker run -d --name research-caddy --restart unless-stopped --network research \
  -p 80:80 -p 443:443 -v "$DIR/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" -v research_caddy:/data caddy:2 >/dev/null

say "Checks"
IP=$(curl -fsS https://api.ipify.org || echo "?")
A=$(dig +short A "$HOST" | tail -1)
PTR=$(dig +short -x "$IP" | sed 's/\.$//')
echo "Public IP ............ $IP"
echo "DNS $HOST ... ${A:-missing}$([ "$A" = "$IP" ] && echo '  OK' || echo '  -> add an A record pointing to the IP')"
echo "Reverse DNS (PTR) .... ${PTR:-missing}$([ "$PTR" = "$HOST" ] && echo '  OK' || echo "  -> set it to $HOST in your provider's panel")"
if timeout 6 bash -c '</dev/tcp/gmail-smtp-in.l.google.com/25' 2>/dev/null; then
  echo "Outbound port 25 ..... OPEN"
else
  echo "Outbound port 25 ..... BLOCKED -> ask your provider to unblock it (see the guide)"
fi
echo
echo "Put these two values in Railway -> service SCRAPPING -> Variables:"
echo "  VERIFIER_SERVICE_URL   = https://$HOST"
echo "  VERIFIER_SERVICE_TOKEN = $(grep '^VERIFIER_TOKEN=' "$ENV_FILE" | cut -d= -f2)"
echo "(the token is also stored in $ENV_FILE — keep it private)"
