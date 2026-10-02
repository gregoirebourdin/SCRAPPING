# LeadForge — agency lead engine (acquisition agencies × coaches / infopreneurs)

A Clay-like, self-hosted lead machine. It discovers marketing agencies that sell **acquisition** (Meta/Google/YouTube
ads, funnels, lead gen, launches) to **coaches, course creators and infopreneurs**, checks they are alive and
English-speaking, extracts their **email**, **founder + LinkedIn**, a **named coach/infopreneur client** with the
evidence sentence, and that client's **funnel** (type, platform, steps, offer). Everything runs locally with free
libraries — no paid API, no proxy service, no data provider.

```
backend/   Python 3.11 · FastAPI · SQLAlchemy/SQLite · httpx · Playwright (Chromium) · selectolax · spaCy · dnspython
frontend/  Next.js 16 · Tailwind — a minimal table over the API (optional: the CLI + CSV export is the core product)
```

## Pipeline

| stage | what happens | output |
|---|---|---|
| **discover** | ~1 700 queries (31 services × 40 ICP terms, 80 exact-phrase queries, 24 listicle queries, 360 modifier queries, optional `--geo` multipliers) × countries (us/gb/au/ca) × engines (Google via headless Chromium, Bing, DuckDuckGo), best queries first, stopping once the candidate pool is `target × multiplier` → listicle pages ("Top 15 ads agencies for coaches") are expanded → directory seeds (Teachable Experts, `data/seeds/*.txt`) | `candidates` (one row per registrable domain, blocklist of ~250 platforms/social/news/directories) |
| **crawl** | homepage → `sitemap.xml` → up to 14 prioritised pages (case studies, clients, about, contact, services, industries); Chromium only when the static HTML is a JS shell | `page_cache` (zlib, 7-day TTL) |
| **qualify** | liveness (HTTP, parked/expired, thin content, last activity via sitemap `lastmod`/meta dates/copyright) · language (langdetect + `lang` attr) · agency vs SaaS/blog/directory/job board · acquisition services · infopreneur ICP · **score 0-100** | `agencies` with `status` qualified / review / rejected and `reject_reason` |
| **extract** | name/tagline/location/founded/team · emails (mailto, text, Cloudflare `data-cfemail`, `[at] [dot]`) · socials · booking link · tech fingerprints (40 platforms) · **clients**: JSON-LD reviews, testimonial blocks, case-study headings, spaCy PERSON + client/coach context, client logo walls (agency team excluded) · **founder**: LinkedIn `/in/` links, JSON-LD, "Founder & CEO" patterns, slug ↔ name matching | `agency_emails`, `clients`, founder columns |
| **clients** | client name → website (outbound link on the case-study page, else search with strict full-name + ICP verification) → **funnel detection** (webinar / VSL / application / lead magnet / challenge / quiz / low-ticket / course sales…, platform, observed steps: opt-in → video → calendar → checkout, offer, price) | `funnels` |
| **founders** | founder LinkedIn via search-engine result titles ("Name - Founder - Agency \| LinkedIn") — LinkedIn itself is never requested | founder columns |
| **emails** | MX lookup always; SMTP `RCPT TO` when outbound port 25 is open (auto-detected); catch-all detection; `hello@/info@/contact@` guesses flagged as `guessed` | `verification` per email |
| **finalize** | tiers — **A** = email + named coach client (+ funnel) · **B** = email + proven ICP · **C** = review — stats, CSV export | `data/exports/run_<id>_<ts>.csv` |

Every stage is idempotent and resumable: SERPs and pages are cached, candidates/agencies carry their status, so a run
can be stopped (Ctrl+C) and restarted without redoing work. `--reprocess` re-runs extraction on every known candidate
from the cache after you tune the extractors.

### Search engines, honestly

* **Google** needs a real browser and a *residential* IP — from a datacenter it answers with a captcha immediately.
  From your machine it works (persistent cookies, typed queries, consent handled).
* **Bing** works over plain HTTP but, under pressure, silently answers a *truncated* query ("we help coaches…" →
  pages about "we"). The router measures result relevance, discards such answers (never cached), switches Bing to the
  browser transport and cools the engine down.
* **DuckDuckGo** paginates but challenges after a burst; it is paced and cooled down like the others.
* Clutch / Sortlist / GoodFirms / DesignRush sit behind Cloudflare and are not used.

Run from a normal home/office connection, keep the default pacing (`LF_SEARCH_MIN_DELAY=2.5`), and expect roughly
3–6 h for the 1 000-lead configuration and a night for a 10 000-agency crawl (the crawl itself is fast: 24 concurrent
requests, 2 per host).

## Install

```bash
# backend
cd backend
python3.11 -m venv .venv && source .venv/bin/activate      # (uv venv works too)
pip install -e . && pip install -e ".[dev]"
python -m spacy download en_core_web_sm
playwright install chromium                                 # headless browser for Google + JS sites
python -m app.cli doctor                                    # checks browser, engines, DNS, SMTP port 25

# frontend (optional)
cd ../frontend && npm install
```

Configuration is environment-based (`LF_*`, or `backend/.env` — see `backend/.env.example`).

## Use

```bash
cd backend && source .venv/bin/activate

python -m app.cli test-site https://someagency.com          # crawl + extract + score one site, print everything
python -m app.cli run --target 1000                         # full pipeline → data/exports/run_<id>_<ts>.csv
python -m app.cli run --target 10000 --multiplier 6         # max volume: ~60k candidates, every one crawled
python -m app.cli run --target 10000 --geo "london,sydney,toronto,dubai,los angeles,new york,austin,miami"   # + geo-multiplied queries
python -m app.cli run --max-queries 40 --engines bing       # quick test
python -m app.cli run --stages clients,founders,emails,finalize   # re-enrich without re-discovering
python -m app.cli run --reprocess --stages crawl,finalize   # re-extract every known domain from cache
python -m app.cli export leads.csv --tier A,B --min-score 60
python -m app.cli stats
python -m app.cli serve                                     # API on http://localhost:8000 (docs at /docs)
```

Frontend: `cd frontend && npm run dev` → http://localhost:3000 (expects the API on :8000, override with
`NEXT_PUBLIC_API_URL`).

### CSV columns

`tier, score, status, agency_name, website, domain, email, email_verification, email_source, all_emails, founder_name,
founder_title, founder_linkedin, founder_source, linkedin_company, instagram, facebook, youtube, country, city,
services, icp_signals, tech, booking_url, phone, last_activity, client_name, client_role, client_niche,
client_website, client_evidence, client_evidence_url, clients_count, funnel_url, funnel_type, funnel_platform,
funnel_offer, funnel_price, funnel_steps, tagline, description, pages_crawled, key_pages, reject_reason`

`email_verification`: `smtp_valid` (mailbox confirmed) · `catch_all` (domain accepts anything) · `mx_valid` (mail server
exists; SMTP not possible from your network) · `no_mx` / `invalid` (never send) · `guessed` emails are in
`email_source`.

## Tuning

* `backend/app/lexicon.py` — every keyword list: services, ICP terms, coach roles, platform fingerprints, funnel types,
  query matrix, blocklists. This is where lead quality is tuned.
* `backend/app/qualify/scoring.py` — weights and gates (`LF_MIN_LEAD_SCORE`, `LF_REQUIRE_NAMED_CLIENT`).
* `backend/app/util/urls.py` — domain blocklist.
* `python -m app.cli test-site <url>` after each change, then `run --reprocess --stages crawl,finalize` to re-score
  everything from cache.

## Volume

Discovery stops when the candidate pool reaches `target × multiplier` or the query matrix is exhausted. To go past
~10 000 candidates: add `--geo` cities, add ICP/service terms in `lexicon.py` (every new term multiplies the matrix),
drop extra `data/seeds/*.txt` lists, and run with all three engines from a residential connection. The crawl stage
processes *every* candidate (not just until the target) unless `--stop-at-target` is given, so volume is bounded by
discovery, never by qualification.

## Tests

```bash
cd backend && python -m pytest -q && ruff check app tests     # 20 tests: unit + offline integration (fixture site, pipeline, API)
```

## Honest limits

* Named client + funnel is found for roughly half of the qualified agencies — many agencies never name a client.
  Tier B leads (email + proven ICP) are still good outreach targets.
* `mx_valid` means the mail server exists, not that the mailbox does; run from a network with outbound port 25 open
  to get `smtp_valid`.
* Founder LinkedIn resolution through search engines depends on Google being available (Bing drops the `site:` and
  quoted operators under pressure); links found on the agency site itself are always reliable.
