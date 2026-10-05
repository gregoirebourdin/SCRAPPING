# Scout — Architecture

Scout is an AI-native B2B lead intelligence workspace. A user describes an ICP in
natural language; Scout plans a campaign, discovers companies from appropriate
sources, crawls them, resolves decision makers, finds and verifies professional
emails, scores every candidate, attaches field-level evidence and streams
**qualified, never-seen-before** leads into a fast table that the AI chat can operate.

This document is the entry point. Companion documents:

| Document | Contents |
|---|---|
| [DATABASE.md](DATABASE.md) | Full relational schema, indexes, identity & dedupe keys, registry/exposure model |
| [PIPELINE.md](PIPELINE.md) | Campaign engine, job queue, state machines, discovery → qualification funnel, enrichment engine |
| [AI_TOOLS.md](AI_TOOLS.md) | AI provider abstraction, model roles, chat operator, tool contracts, prompt-injection defense |
| [DESIGN_SYSTEM.md](DESIGN_SYSTEM.md) | Tokens, typography, components, interaction rules, screen inventory |
| [SECURITY.md](SECURITY.md) | Auth, workspace isolation, SSRF, CSV injection, secrets, compliance |

---

## 1. Principles (from the product spec, restated as engineering rules)

1. **Qualified, not scraped.** Campaign targets count rows that passed the quality gate.
2. **UNKNOWN beats WRONG.** No fabricated people, emails or facts. Every factual field
   carries provenance (source, URL, observed_at, confidence).
3. **~90 % deterministic / ~10 % AI.** AI is used for language understanding, semantic
   classification, ambiguous extraction, hard research and generated copy only.
4. **Filter early, enrich late.** Cheap checks (registry, exclusion, keyword) always run
   before expensive ones (crawl, AI, SMTP).
5. **Domain-centric.** One crawl per domain, reused by every person, column and campaign.
6. **Postgres is the source of truth** — for entities, campaign state, jobs and events.
7. **Lean infrastructure.** Vercel + Neon + Railway + Gemini + OSS components. No Redis,
   Kafka, Elastic, vector DB or paid enrichment API until measurements require it.

The four foundational systems that must never be sacrificed:
**global lead registry, exclusion system, provenance system, dynamic enrichment engine.**

---

## 2. Production topology

```
                 ┌──────────────────────────── Vercel ────────────────────────────┐
 Browser ──────► │ Next.js 16 (App Router, RSC)                                     │
  (cookies)      │  • UI shell, table, chat panel                                   │
                 │  • Better Auth (email+password, optional Google OAuth)           │
                 │  • BFF proxy  /api/v1/*  → mints 60 s service JWT → FastAPI      │
                 │  • SSE proxy  /api/v1/events/stream (auto-reconnect, Last-Event) │
                 └───────────────┬──────────────────────────────────────────────────┘
                                 │ HTTPS + signed JWT (HS256, aud=scout-api)
                 ┌───────────────▼───────────────── Railway ────────────────────────┐
                 │ Service A  apps/api  (FastAPI + async workers, one process)      │
                 │  • REST API, chat operator (Gemini function calling, SSE)        │
                 │  • Postgres job queue (FOR UPDATE SKIP LOCKED, leases)           │
                 │  • campaign engine, discovery, crawler, extraction, email        │
                 │    finder, scoring, enrichment engine                            │
                 │        │ private network (*.railway.internal)                    │
                 │  ┌─────▼──────────────────────┐  ┌────────────────────────────┐  │
                 │  │ Service B services/        │  │ Service C services/        │  │
                 │  │ email-verifier (Go)        │  │ maps-scraper (gosom,       │  │
                 │  │ AfterShip verifier +       │  │ pinned image, web API)     │  │
                 │  │ wappalyzergo fingerprints  │  │ optional, on demand        │  │
                 │  └────────────────────────────┘  └────────────────────────────┘  │
                 └───────────────┬──────────────────────────────────────────────────┘
                                 │ TLS
                 ┌───────────────▼──────────┐       ┌──────────────────────────────┐
                 │ Neon Postgres 16+        │       │ Gemini API (google-genai)    │
                 │ pooled URL → API runtime │       │ structured output, function  │
                 │ direct URL → migrations, │       │ calling, Google Search       │
                 │ LISTEN/NOTIFY            │       │ grounding, URL context       │
                 └──────────────────────────┘       └──────────────────────────────┘
```

### Why this shape

* **Vercel never scrapes.** Vercel functions only authenticate, render and proxy.
  All long-running work runs on Railway workers.
* **FastAPI is the single domain API and the only writer of business tables.** The Next.js
  app touches Postgres only through Better Auth (its own auth tables). This keeps one
  source of business logic, one migration tool (Alembic), and one place where workspace
  authorization is enforced.
* **The browser never talks to FastAPI directly.** The BFF proxy keeps the API secret,
  JWT and FastAPI URL server-side. FastAPI is public on Railway only because Vercel is not
  on Railway's private network; every request must carry a short-lived JWT signed with
  `INTERNAL_API_SECRET`.
* **Service B is separate** because the best permissively licensed components for email
  verification (AfterShip/email-verifier, MIT) and tech fingerprinting
  (projectdiscovery/wappalyzergo, MIT) are Go libraries, and because SMTP verification needs
  infrastructure that permits outbound port 25 (see Risks). Both live in one small Go
  binary to avoid a fourth paid service. The API degrades gracefully when it is absent
  (built-in MX/syntax verifier, built-in signature detector).
* **Service C is optional.** gosom/google-maps-scraper (MIT) runs unmodified as an isolated
  adapter, reached over private networking; it is only needed for local-business ICPs and
  can be scaled to zero.

---

## 3. Verified dependency baseline (checked 2026-10-05)

| Area | Choice | Version verified | Notes |
|---|---|---|---|
| Web framework | Next.js (App Router) | 16.3.8 (`latest`, Active LTS line 16.x) | `proxy.ts` replaces `middleware.ts`; async request APIs only; Turbopack default; `next lint` removed (ESLint flat config) |
| UI runtime | React / React DOM | 19.3.0 | |
| Language | TypeScript | 5.9.x (strict) | TS 7.0 (Go-native) is `latest`, but Next 16 type-check tooling and several plugins still target the 5.x API; pinned to 5.9 for stability |
| Styling | Tailwind CSS (+ `@tailwindcss/postcss`) | 4.3.3 | CSS-first `@theme`, tokens as CSS variables |
| Primitives | Radix UI (`radix-ui`) | 1.7.0 | unstyled primitives only |
| Table | TanStack Table | 9.2.6 | v9 `useTable` + `tableFeatures` API |
| Virtualization | TanStack Virtual | 3.14.13 | |
| Data fetching | TanStack Query | 5.104.1 | |
| Validation | Zod | 4.6.5 | |
| Forms | React Hook Form / resolvers | 7.89.0 / 5.9.1 | |
| Icons | lucide-react | 1.52.0 | |
| Animation | motion | 14.0.0 | |
| Command palette | cmdk | 1.1.1 | |
| Font | geist | 1.7.2 | Geist Sans + Geist Mono |
| Auth | better-auth | 1.7.7 | self-hosted, Postgres via `pg` Pool, no paid service |
| E2E | @playwright/test | 1.63.0 | |
| Types from OpenAPI | openapi-typescript | 7.13.0 | |
| Next DevTools MCP | next-devtools-mcp | 0.4.0 | configured in `.mcp.json` |
| API | FastAPI / uvicorn | 0.142.2 / 0.54.0 | |
| Validation | Pydantic / pydantic-settings | 2.13.5 / 2.15.0 | |
| ORM / migrations | SQLAlchemy / Alembic | 2.1.3 / 1.20.0 | asyncpg 0.31.0 driver |
| AI SDK | google-genai | 2.28.0 | unified Gemini SDK |
| HTTP | httpx | 0.28.1 | custom SSRF-safe network backend |
| HTML parsing | selectolax / lxml | 1.0.0 / 6.1.3 | |
| JS rendering tier | Crawl4AI / Playwright | 0.9.4 (Apache-2.0) / 1.63.0 | optional extras, only when HTTP fails |
| DNS | dnspython | 2.8.0 | MX lookups |
| Domain parsing | tldextract | 5.4.0 | registrable domain via PSL |
| Fuzzy match | rapidfuzz + pg_trgm | 3.14.6 | |
| Email verification | AfterShip/email-verifier (Go) | v1.5.0 (MIT) | Service B |
| Tech detection | projectdiscovery/wappalyzergo (Go) | v0.3.4 (MIT) | Service B |
| Maps discovery | gosom/google-maps-scraper | MIT, `/api/v1/jobs` REST API | Service C |
| French registry | recherche-entreprises.api.gouv.fr | public, free, ~7 req/s | NAF codes, headcount bands, directors |

### Gemini models (verified on ai.google.dev, 2026-10-05)

Model names are **never hard-coded in business logic**. `scout/ai/models.py` resolves roles
from environment variables with these defaults:

| Role | Default | Price (paid tier, per 1M in/out) | Used for |
|---|---|---|---|
| `models.fast` | `gemini-3.1-flash-lite` | $0.25 / $1.50 | routine semantic classification |
| `models.extractor` | `gemini-3.1-flash-lite` | $0.25 / $1.50 | structured extraction from cached chunks |
| `models.reasoning` | `gemini-3.8-flash` | $0.75 / $3.75 (intro price to 2026-12-31) | chat operator, ICP parsing, column planning |
| `models.search` | `gemini-3.8-flash` | same + Google Search grounding: 5,000 free req/month (shared across Gemini 3.x), then $14 / 1,000 | grounded web research, discovery fallback |

---

## 4. Repository structure

```
/apps
  /web                      Next.js 16 app (Vercel)
  /api                      FastAPI + workers (Railway service A), Python 3.12, uv
/services
  /email-verifier           Go service: /v1/verify, /v1/catch-all, /v1/tech (Railway service B)
  /maps-scraper             Deployment wrapper for gosom/google-maps-scraper (Railway service C)
/packages
  /design-system            tokens.css, Tailwind theme, React primitives
  /schemas                  openapi.json (generated from FastAPI) + generated TS types
  /shared                   TS constants, formatters, filter-operator metadata
/infrastructure             docker-compose (local stack), Railway/Vercel/Neon notes, scripts
/benchmarks                 manually validated quality benchmark + runner
/docs                       this documentation
```

Boundaries:

* `packages/schemas` is **generated** (`pnpm schemas:generate`): Pydantic → OpenAPI →
  TypeScript. CI fails if the committed output is stale. AI tool JSON schemas are generated
  from the same Pydantic models, so the frontend, API and AI tool contracts cannot drift.
* `apps/web` imports domain types only from `@scout/schemas`, never re-declares them.
* Source adapters live behind `scout.discovery.base.DiscoverySource`; nothing outside
  `scout/discovery/` knows source-specific fields.
* Verification, tech detection and AI providers live behind interfaces
  (`EmailVerifier`, `TechDetector`, `AIProvider`) with swap-in implementations.

---

## 5. Key request flows

### 5.1 Authentication & workspace context

1. Better Auth (in Next.js) authenticates the user and sets an httpOnly session cookie.
2. `proxy.ts` redirects unauthenticated page requests to `/sign-in` (cookie presence check);
   route handlers re-validate the session server-side (`auth.api.getSession`).
3. The BFF route `app/api/v1/[...path]/route.ts` mints a 60-second HS256 JWT
   `{sub: user.id, email, name, aud: "scout-api", iss: "scout-web"}` and forwards the request
   with `Authorization: Bearer …` and `X-Workspace-Id` (from the `scout_ws` cookie).
4. FastAPI verifies the JWT, loads the membership row for `(workspace_id, user_id)` and builds
   a `WorkspaceContext(user_id, workspace_id, role)`. **Every** repository query takes this
   context and filters by `workspace_id`. Missing membership → 403. On first login a personal
   workspace is bootstrapped.

### 5.2 Table data

`GET /v1/lists/{id}/rows?view=…&filters=…&sort=…&cursor=…&limit=500` → keyset (cursor)
pagination over a SQL query compiled by the filter engine (`scout/query/`). The client
virtualizes rows (TanStack Virtual) and fetches further pages as the user scrolls. Filters
and sorts run server-side on indexed columns; custom-column filters join
`custom_field_values` on `(column_id, entity_id)`.

### 5.3 Live progress (SSE)

Workers append to `job_events` (bigserial id) and `NOTIFY scout_events`. FastAPI exposes
`GET /v1/events/stream` (SSE, `id:` = event id, honours `Last-Event-ID`, 15 s heartbeats).
The Next.js proxy streams it to the browser `EventSource`. Vercel's function duration cap
simply causes a reconnect; no events are lost because the cursor is the event id.
The table never reorders under the user: new leads surface as a “+N new leads” pill.

### 5.4 Chat operator

`POST /v1/chat/threads/{id}/messages` streams SSE frames: `text`, `tool_call`, `tool_result`
(rendered as compact cards), `ui_effect` (filter/sort/select/hide applied client-side),
`confirm` (destructive action awaiting approval), `done`. The request carries the UI
context (list, view, filters, visible columns, selected row ids). Tools execute server-side
with Pydantic validation, workspace authorization, audit logging and undo payloads.

### 5.5 Campaign lifecycle (summary — details in PIPELINE.md)

prompt → ICP parser (Gemini structured output, deterministic fallback) → `CampaignDefinition`
(Pydantic, persisted normalized into `campaigns`, `campaign_filters`, `campaign_sources`,
`campaign_exclusions`) → interpretation card → `campaign.plan` job → discovery jobs per
source → per-company `company.process` jobs (checkpointed stages) → qualified leads inserted
into the target list → `campaign.tick` evaluates yield, stop conditions and adaptive sourcing
until `qualified ≥ target` or an explicit stop reason.

---

## 6. State machines

### 6.1 Campaign

```
 draft ──► planning ──► running ◄──► paused
                          │  │
                          │  ├──► completed        (qualified ≥ target)
                          │  ├──► exhausted        (all eligible sources exhausted)
                          │  ├──► budget_reached   (campaign max cost / workspace hard cap)
                          │  ├──► limit_reached    (safety limits: max raw candidates, max runtime)
                          │  └──► failed           (unrecoverable planning error)
 any non-terminal ───────────► cancelled           (user)
```
Every terminal state stores `stop_reason` (human readable) and `stopped_at`.
A server restart never loses state: status lives in Postgres and work lives in `jobs`.

### 6.2 Job

```
 pending ──claim──► claimed ──start──► running ──ok──► completed
    ▲                  │                  │
    │ lease expired    │                  ├─ retryable error, attempts < max ──► retrying ──(run_after)──► claimed
    └──────────────────┴──────────────────┤
                                          ├─ retryable error, attempts = max ──► dead_letter
                                          ├─ permanent error ──────────────────► failed
 pending/retrying ──campaign paused──► paused ──resume──► pending
 any non-terminal ──cancel──► cancelled
```
Leases (default 120 s) are extended by heartbeats; a reaper re-queues expired leases, so a
crashed worker never loses a job. Exponential backoff with full jitter, bounded attempts,
categorized errors (`network`, `timeout`, `blocked`, `rate_limited`, `not_found`, `parse`,
`ai`, `validation`, `budget`, `internal`). Blocked pages are not retried indefinitely.

### 6.3 Enrichment cell

```
not_started ─► queued ─► running ─► success | unknown | failed
                                      │
                         (observed_at older than refresh policy) ─► stale ─► queued
```
`unknown` (insufficient evidence) is distinct from a `false` value and from `failed`
(technical error with a retryable reason shown in the UI).

### 6.4 Email status

`SAFE | RISKY | CATCH_ALL | UNKNOWN | INVALID` with the raw signals kept separately
(`mx_valid`, `smtp_result`, `catch_all`, `disposable`, `role_address`, `free_provider`,
`pattern_confidence`, `overall_confidence`, `last_checked_at`). A catch-all domain never
yields `SAFE` for a guessed address.

---

## 7. Global dedupe & exclusion strategy (summary)

* **Registry ≠ lists.** `companies` and `people` are canonical per workspace and are never
  deleted when a list row is removed. `lead_exposures` records every user-facing contact
  with an entity (`DISCOVERED`, `SHOWN`, `ADDED_TO_LIST`, `IMPORTED`, `EXPORTED`,
  `CONTACTED`, `ENRICHED`). `*_discovery_events` record every system-level sighting with its
  outcome (qualified, rejected + reason, duplicate, excluded…).
* **Company identity:** registrable domain (PSL) → official identifier (e.g. SIREN) →
  normalized name + city (trigram, only when no stronger key). Unique
  `(workspace_id, normalized_domain)` and `(workspace_id, registry_source, registry_id)`.
* **Person identity:** verified email → public profile URL → company + normalized full name
  (+ role). Never by name alone across companies.
* **Exclusion modes** compile into explicit `ExclusionRule`s
  (`entity`, `exposure_types`, `within_days`, `list_ids`, `campaign_ids`) evaluated in SQL,
  in batches, *before* any crawl or AI spend. Company-level and person-level rules are
  independent, so “new people at known companies” is native.
* **Atomic reservations:** a candidate entering qualification takes a row in
  `campaign_reservations` guarded by a partial unique index on active holds; holds expire.
  The qualifying transaction writes exposures and converts the hold atomically, so two
  concurrent campaigns cannot both deliver the same “new” person.
* **Suppression** is checked first and always wins.

Details: DATABASE.md §4 and PIPELINE.md §5.

---

## 8. Cost model (target ≈ 3,000 qualified leads / month)

| Item | Assumption | Monthly |
|---|---|---|
| Vercel | Hobby (personal) or Pro if used commercially ($20) | $0 – $20 |
| Neon | Launch plan, ~1–2 GB storage, scale-to-zero compute | ~$5 – $15 |
| Railway | API+workers (~0.5 vCPU / 1 GB avg) + Go verifier (tiny) | ~$5 – $15 usage |
| Gemini | ~25k flash-lite calls × ~3k tokens + chat on 3.8-flash | ~$3 – $8 |
| Google Search grounding | mostly inside the 5,000 free requests | $0 – $5 |
| Discovery/crawl/registry | open source, public endpoints | $0 |

Total ≈ **$15 – $40**. Cost per qualified lead is tracked live (`usage_events`) and guarded
by workspace monthly budgets and per-campaign hard caps.

---

## 9. Technical risks & mitigations

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| R1 | **Outbound SMTP blocked on Railway Hobby** (ports 25/465/587/2525 are Pro-only as of 2026) | Without SMTP probing, guessed emails cannot reach `SAFE`; campaigns requiring SAFE emails stall | Verifier is pluggable; `SMTP_ENABLED=false` gives honest `UNKNOWN`/`RISKY` statuses; published emails still reach `SAFE`; the UI reports “SMTP verification unavailable”. Options: Railway Pro, or run Service B on a small VPS that permits port 25 |
| R2 | SMTP probing from datacenter IPs is greylisted / accept-all (M365, Google) | Many domains look catch-all | Catch-all detection with random recipients; `RISKY` + pattern confidence; domain pattern memory improves over time |
| R3 | Google Maps scraping fragility / blocking | Local-business discovery drops | Isolated adapter, source health metrics, automatic deprioritization, alternative sources (registry, OSM, web search, grounding) |
| R4 | DuckDuckGo HTML endpoint rate limits | Free web-search adapter fails | Health tracking, backoff, Gemini grounding fallback within budget |
| R5 | Grounded search cost beyond free tier | Budget overrun | Cost-class planner: grounding only when cheaper resolvers cannot answer; hard caps |
| R6 | Hallucinated people/facts | Trust destroyed | Evidence required: AI-extracted names must literally appear in the cited page; grounded answers without sources are `UNKNOWN`/low confidence |
| R7 | Prompt injection in scraped pages | Tool misuse, data exfiltration | Enrichment calls have no tools; content wrapped as untrusted data; chat tools need server-side authorization; destructive tools require confirmation |
| R8 | SSRF via hostile URLs/redirects | Internal network access | DNS/IP validation at connect time (rebinding-safe), per-hop redirect checks, scheme/port allow-list |
| R9 | Neon pooled connections & LISTEN | NOTIFY not delivered through PgBouncer | Workers LISTEN on the direct URL; polling fallback every second |
| R10 | Vercel function duration on SSE | Stream cut | Auto-reconnect with `Last-Event-ID`; events are durable rows |
| R11 | Registry/maps data without websites | Lower crawl coverage | Website resolution step (domain candidates + DNS + on-site SIREN/name verification + search fallback) |
| R12 | Storage growth of crawl cache | Neon cost | Cleaned text only (capped), head-HTML only for home page, content hashing, TOAST compression, retention policy |
| R13 | GDPR / B2B prospecting compliance | Legal | Public sources only, provenance + collection date stored, suppression/opt-out, no authenticated LinkedIn scraping, documented in SECURITY.md |
| R14 | TanStack Table v9 is recent | API churn | Table logic isolated in `components/table/`; v9 stable line pinned |

---

## 10. Phases

| Phase | Scope | Status tracked in |
|---|---|---|
| 0 | This architecture set | `docs/` |
| 1 | Product shell: auth, workspace, sidebar, lists, table, chat panel, drawer, command palette, seed data, saved views, responsive | `apps/web`, `apps/api` |
| 2 | Registry, memberships, discovery history, exposures, suppression, dedupe, import/export | `scout/services/registry.py`, `exclusion.py` |
| 3 | Chat operator & tools | `scout/chat/` |
| 4 | Discovery: campaign engine, router, adapters, canonicalization, adaptive sourcing | `scout/pipeline/`, `scout/discovery/` |
| 5 | Crawling tiers, cache, evidence | `scout/crawl/` |
| 6 | People: extraction, titles, grounded fallback, confidence, dedupe | `scout/extract/` |
| 7 | Email: published, pattern memory, permutations, MX, SMTP, catch-all | `scout/email/`, `services/email-verifier` |
| 8 | Qualification: scoring, quality gate, live progress, completion | `scout/pipeline/scoring.py` |
| 9 | Dynamic enrichment engine | `scout/enrich/` |
| 10 | Hardening: retries, source health, budgets, benchmark, load tests, observability | across |
