# searxng — Railway service D (free web search)

Deployment wrapper for [SearXNG](https://github.com/searxng/searxng) (AGPL-3.0-or-later), run **unmodified** from
the official image with our `settings.yml`. The API's search layer (`apps/api/scout/search/`) queries its JSON
API over Railway private networking. It is the first web search engine of the product; Gemini Google Search
grounding is only the fallback when free search cannot resolve a question.

```
deterministic / public sources → cached data → crawl → SearXNG (→ DuckDuckGo HTML) → Gemini grounding (unresolved only)
```

## Why a separate, isolated service

* **Replaceable.** The API talks to a `WebSearchProvider` interface (`scout/search/types.py`); SearXNG is one
  adapter (`scout/search/searxng.py`). Swapping it for another engine/API touches one adapter, not the pipeline.
* **Licence hygiene.** AGPL code stays in its own process; the API is a plain HTTP client (see below).
* **Blast radius.** Upstream engines (Bing, Brave, DuckDuckGo…) rate-limit or CAPTCHA datacenter IPs. SearXNG
  suspends a failing engine on its own; when it returns nothing but errors, the API marks the provider blocked,
  cools it down and falls back to the DuckDuckGo HTML adapter, then — only if still unresolved — to Gemini.
* **Cost.** ~150–250 MB RAM, no database (limiter off ⇒ no Valkey). App Sleeping is on.

## API used by the adapter

`GET /search?q=…&format=json&pageno=1&language=fr-FR&safesearch=0&categories=general[&time_range=month][&engines=…]`
→ `{"query", "results": [{"url", "title", "content", "engine", "engines", "positions", "score",
"publishedDate"}…], "unresponsive_engines": [["brave", "too many requests"]…], …}`.
`GET /healthz` → `OK` (Railway health check). `site:` filters are passed inside `q`.

## Configuration (`settings.yml`)

| Setting | Value | Why |
|---|---|---|
| `use_default_settings.engines.keep_only` | duckduckgo, brave, bing (on) · google, yahoo, qwant (loaded, off) · wikipedia, wikidata · duckduckgo/bing/google news, brave.news | Company websites, LinkedIn profile titles, press. Startpage and Mojeek are `inactive` upstream (proof-of-work CAPTCHA). Google is disabled upstream (JS/CAPTCHA from datacenter IPs): enable it only if it works from your egress IP. |
| `search.formats` | `[html, json]` | JSON is required by the API (403 otherwise). |
| `server.limiter` | `false` | Private network only; the API is the only client and bot detection would block it. |
| `server.secret_key` | placeholder `ultrasecretkey` | Overridden by `SEARXNG_SECRET`; SearXNG refuses to start with the placeholder, so a missing secret fails loudly. |
| `search.safe_search` | `0` | B2B research; the API sends `safesearch` explicitly anyway. |
| `outgoing.request_timeout` / `max_request_timeout` | 4 s / 8 s | Per-engine; the API waits up to `SEARXNG_TIMEOUT` (10 s) for the merged page. |
| `search.suspended_times` | CAPTCHA 1 h, 429 5 min, access denied 10 min | Shorter than upstream so engines come back during long campaigns. |
| `plugins` | tracker URL remover only | Cleaner result URLs (provenance). |

## Deploy on Railway

1. New service → *Deploy from repo*, **root directory `services/searxng`** (Dockerfile builder, `railway.json`).
   Name it `searxng` so its private host is `searxng.railway.internal`.
2. **Networking: do not generate a public domain.** The instance has no authentication and the limiter is off.
3. Variables on the searxng service:
   * `SEARXNG_SECRET` = a random 32+ byte string (`openssl rand -hex 32`). **Required** — the service exits without it.
   * (optional) `SEARXNG_BASE_URL=http://searxng.railway.internal:8080/`
4. Variables on the API service (and the worker, if separate):

   ```
   SEARXNG_URL=http://searxng.railway.internal:8080
   # optional:
   SEARXNG_TIMEOUT=10
   SEARXNG_MAX_CONCURRENCY=4
   SEARCH_PROVIDERS=searxng,duckduckgo          # web_search discovery source, in order
   SEARCH_LOOKUP_PROVIDERS=searxng              # people discovery, enrichment research, Gemini pre-check
   GEMINI_SEARCH_FALLBACK_ONLY=true             # false = legacy (Gemini grounding without the free pass)
   ```

   Unset `SEARXNG_URL` to disable SearXNG entirely: discovery falls back to DuckDuckGo HTML and lookups go
   straight to Gemini as before.
5. App Sleeping is enabled in `railway.json`: the first query after idle wakes the service (cold start of a
   few seconds). A timeout during wake-up just makes that query fall back (DuckDuckGo / Gemini); set
   `"sleepApplication": false` if campaigns run continuously.

Resources: 0.5 vCPU / 512 MB is plenty.

Local run (Docker):

```bash
docker build -t scout-searxng services/searxng
docker run --rm -p 8080:8080 -e SEARXNG_SECRET="$(openssl rand -hex 32)" scout-searxng
curl 'http://localhost:8080/search?q=agence+marketing+lyon&format=json&language=fr-FR' | jq '.results[:3]'
SEARXNG_URL=http://localhost:8080 uv run ...   # from apps/api
```

## Upgrading

The image is pinned by tag **and** digest (`2026.10.4-d48c4b555`, published 2026-10-04; SearXNG ships rolling
builds, no releases). To upgrade: pick a recent tag
(`curl -s "https://hub.docker.com/v2/repositories/searxng/searxng/tags?page_size=10"` — the `digest` field of
`/tags/<tag>` is the multi-arch index digest), check the upstream commit log for engine / settings changes,
update both tag and digest, run `uv run pytest tests/unit/search` (contract tests on the JSON payload), deploy,
then check `/stats` (private) for engine error rates.

## Licence (AGPL-3.0-or-later)

* We run the **unmodified** upstream image as a separate network service and only provide configuration
  (`settings.yml`). The API communicates with it over HTTP at arm's length; that does not make the API a
  derivative work, and no Scout code is linked into SearXNG.
* AGPL §13 (offer the source to users interacting with the program over a network) concerns *modified*
  versions. We do not modify it, and only our API (not end users) talks to it on a private network. The
  corresponding source is upstream's public repository at the pinned commit (`d48c4b555`); this directory
  (our configuration) is the only addition and contains no secrets.
* If SearXNG code is ever patched (custom engines, templates, plugins), those changes must be published under
  AGPL-3.0, and doubly so if the instance is ever exposed to users. Prefer configuration over patches.
* Upstream engines scrape third-party search engines whose terms may forbid automated queries. Keep volumes
  modest (per-lead lookups are cached and bounded), do not add proxy rotation to evade blocking, and treat
  enabling more engines (e.g. Google) as an operator decision.
