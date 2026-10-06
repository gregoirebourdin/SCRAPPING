# email-verifier (Scout service B)

Small Go service that wraps two MIT-licensed libraries behind an authenticated JSON API:

* [AfterShip/email-verifier](https://github.com/AfterShip/email-verifier) **v1.5.0** — syntax,
  disposable / free / role flags, MX lookup, optional SMTP `RCPT` probe and catch-all detection.
* [projectdiscovery/wappalyzergo](https://github.com/projectdiscovery/wappalyzergo) **v0.3.4** —
  technology fingerprints from response headers + HTML already fetched by the crawler.

The service never fetches URLs and never sends `DATA`. The Python API (`scout.email.verifier`)
treats it as optional: when it is absent or failing, each call falls back to the builtin
verifier (syntax + MX, SMTP only when `SMTP_ENABLED=true` on the API).

Both pinned libraries declare `go 1.25.0`, so the module requires Go ≥ 1.25.

## Endpoints

All `/v1/*` routes require `Authorization: Bearer $VERIFIER_TOKEN` when the token is set.

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/healthz` | – | `{"ok":true,"smtp_enabled":false}` (no auth) |
| POST | `/v1/verify` | `{"email":"marie@agence-x.fr"}` | see below |
| POST | `/v1/catch-all` | `{"domain":"agence-x.fr"}` | `{"domain","catch_all":true\|false\|null,"smtp_enabled"}` |
| POST | `/v1/tech` | `{"url","headers":{"name":"value" or ["v1","v2"]},"html"}` | `{"url","technologies":[{"name","categories":[…],"version"}]}` |

`/v1/verify` response:

```json
{
  "email": "marie@agence-x.fr",
  "syntax_valid": true,
  "has_mx": true,
  "mx_hosts": ["mx1.agence-x.fr"],
  "smtp": {"enabled": true, "host_exists": true, "deliverable": true, "full_inbox": false,
           "catch_all": false, "disabled": false, "error": null},
  "disposable": false,
  "role_account": false,
  "free": false,
  "reachable": "yes",
  "error": null
}
```

* `smtp.catch_all` is `null` when SMTP is disabled or the session failed.
* Disposable domains are never resolved or contacted.
* AfterShip reports a domain as catch-all unless the random recipient is explicitly refused, so
  greylisting servers can look catch-all; Scout never marks such guesses `SAFE`.

Errors: `400` invalid body, `401` bad token, `503` concurrency limit reached before the request
deadline, `504` verification exceeded `REQUEST_TIMEOUT`.

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8080` | Listen port (Railway injects it) |
| `VERIFIER_TOKEN` | – | Bearer token; set the same value as `VERIFIER_SERVICE_TOKEN` on the API |
| `SMTP_ENABLED` | `false` | Enable RCPT probing + catch-all detection (needs outbound port 25) |
| `SMTP_HELO_DOMAIN` | `scout.example` | EHLO name — use a domain whose A/PTR points at the egress IP |
| `SMTP_FROM` | `verify@scout.example` | `MAIL FROM` address (a real, monitored domain) |
| `SMTP_TIMEOUT` | `10s` | Connect and per-command SMTP timeout |
| `REQUEST_TIMEOUT` | `45s` | End-to-end budget per request |
| `MAX_CONCURRENCY` | `16` | Simultaneous verifications / fingerprints |

Logs are JSON lines on stdout. `SIGTERM` triggers a graceful shutdown (20 s drain).

## Deploying on Railway

1. New service from this directory (`railway.json` selects the Dockerfile and the `/healthz` check).
2. Do **not** expose a public domain. The API reaches it over private networking, e.g.
   `VERIFIER_SERVICE_URL=http://email-verifier.railway.internal:8080` (listen on `PORT`; Railway
   private networking is IPv6-capable and Go's `:PORT` listener binds dual-stack).
3. Set `VERIFIER_TOKEN` here and `VERIFIER_SERVICE_TOKEN` on the API.

**SMTP needs outbound port 25.** Railway blocks SMTP ports on the Hobby plan (Pro only as of 2026).
Either run on Railway Pro, or deploy this container on a small VPS whose provider allows port 25
(with matching PTR/HELO), and point `VERIFIER_SERVICE_URL` at it over TLS. Without SMTP the service
still provides syntax/MX/disposable/free/role checks and tech fingerprints, and Scout reports
guessed addresses honestly as `UNKNOWN`/`RISKY`.

## Development

```sh
go vet ./... && go test ./...
docker build -t scout-email-verifier . && docker run --rm -p 8080:8080 scout-email-verifier
curl -s localhost:8080/healthz
```
