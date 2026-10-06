# maps-scraper — Railway service C

Deployment wrapper for [gosom/google-maps-scraper](https://github.com/gosom/google-maps-scraper) (MIT),
run **unmodified** in web mode. Scout's `google_maps` discovery adapter
(`apps/api/scout/discovery/google_maps.py`) talks to its REST API over Railway private networking.

## Why a separate, isolated service

* **Different runtime.** The scraper is a Go binary driving headless Chromium (Playwright). Its image is large and
  memory-hungry; keeping it out of the API image keeps service A small and fast to deploy.
* **Blast radius.** Browser automation against Google Maps is the most fragile and most likely to be blocked
  of all sources. Isolated, it can crash, be rate-limited or be switched off without touching the API; the adapter
  reports failures to source health (`scout/discovery/health.py`), which cools the source down and lets the
  router fall back to the registry, OpenStreetMap, web search or grounded search.
* **Cost.** Only local-business ICPs need it, so it can sleep (scale to zero) most of the time.
* **Licensing hygiene.** Upstream is used as a black box through its documented API; no code is vendored.

## API used by the adapter

| Call | Purpose |
|---|---|
| `POST /api/v1/jobs` `{"Name", "keywords": [...], "lang", "zoom", "lat", "lon", "fast_mode", "radius", "depth", "email", "extra_reviews", "max_time" (seconds), "proxies"}` → `201 {"id"}` | create a scrape job (one per industry × city) |
| `GET /api/v1/jobs/{id}` → `{"ID", "Name", "Date", "Status": pending\|working\|ok\|failed, "Data"}` | poll (bounded backoff; the discovery job reschedules itself while the scrape runs) |
| `GET /api/v1/jobs/{id}/download` → CSV | results (`title, website, phone, complete_address, place_id, …`) |
| `DELETE /api/v1/jobs/{id}` | cleanup after download (best effort) |

Scout always sends `email: false` (it finds emails itself, on the company website) and `extra_reviews: false`.

## Deploy on Railway

1. New service → *Deploy from repo*, root directory `services/maps-scraper` (Dockerfile builder, `railway.json`).
2. **Networking: do not generate a public domain.** The service must be reachable only on the private network
   (`<service-name>.railway.internal`). It has no authentication of its own.
3. Variables: `PORT=8080` (already set in the image).
4. Optional: attach a volume at `/gmapsdata` (job metadata + CSVs). Without it, a restart loses in-flight jobs;
   the adapter detects the vanished job (404) and recreates it.
5. Scale to zero: `railway.json` enables *App Sleeping*; the first request after idle wakes the service (expect a
   cold start of a few seconds, covered by the adapter's 30 s job-creation timeout).
6. On the API service (A) set:

   ```
   GMAPS_SCRAPER_URL=http://<service-name>.railway.internal:8080
   POOL_MAPS=1            # one concurrent job creation; jobs themselves run inside service C
   ```

   Unset `GMAPS_SCRAPER_URL` to disable the source entirely (`is_configured()` becomes false and the router
   never selects it).

Resources: 1 vCPU / 1–2 GB RAM is enough for `-c 2`. Raise `-c` (in `CMD`) only with more memory.

Local run:

```bash
docker build -t scout-maps-scraper services/maps-scraper
docker run --rm -p 8080:8080 -v "$PWD/.gmapsdata:/gmapsdata" scout-maps-scraper
GMAPS_SCRAPER_URL=http://localhost:8080 uv run ...
```

## Upgrading

The image is pinned by tag **and** digest (`v1.18.1`, published 2026-09-20). To upgrade: check the upstream
changelog for API/CSV changes, update both tag and digest
(`curl -s "https://hub.docker.com/v2/repositories/gosom/google-maps-scraper/tags?page_size=10"`), run
`uv run pytest tests/unit/discovery/test_google_maps.py`, deploy.

## Legal / ToS caveats

* Automated collection from Google Maps is **not permitted by Google's Terms of Service**. Running this service is
  a business/legal decision for the operator, who is responsible for compliance in their jurisdiction. Prefer
  the registry, OpenStreetMap (ODbL, attribution required) and web search sources where they suffice; the source
  can be excluded per campaign (`sources.excluded: ["google_maps"]`) or disabled globally (unset the URL).
* Only public business listing data is used (name, category, website, phone, address, rating). No reviews, no
  reviewer personal data, no photos are stored (`extra_reviews: false`; the adapter drops those CSV columns).
* Discovered companies keep their provenance (`source_key=google_maps`, listing URL, observed_at) and are
  subject to suppression/opt-out like every other lead (see `docs/SECURITY.md`).
* Keep volumes low and concurrency at 1–2; do not add proxy rotation to evade blocking.
