# Research

<img src="apps/web/public/brand/research-logo-black.svg#gh-light-mode-only" height="40" alt="Research"><img src="apps/web/public/brand/research-logo-white.svg#gh-dark-mode-only" height="40" alt="Research">

AI-native B2B lead intelligence: describe the companies or people you want, and Research discovers,
crawls, qualifies, finds and verifies emails, scores, and delivers **new, evidence-backed** leads
into a fast table operated by an AI chat.

* **Web** — Next.js 16 (App Router), React 19, Tailwind 4, TanStack Table/Virtual/Query, Better Auth → **Vercel**
* **API + workers** — FastAPI, SQLAlchemy 2 (async), Postgres job queue (`SKIP LOCKED`, leases, retries) → **Railway**
* **Database** — Postgres → **Neon** (pooled URL for queries, direct URL for migrations and LISTEN/NOTIFY)
* **AI** — Gemini behind a model-role layer (`fast` / `extractor` / `reasoning` / `search`), deterministic first
* **Optional services** — `services/searxng` (self-hosted web search, used before any Gemini grounding), `services/email-verifier` (Go, AfterShip verifier + tech fingerprints), `services/maps-scraper` (gosom Google Maps scraper)

The codebase keeps its original package name (`scout`, `@scout/*`); the product is **Research**.

Docs: [Architecture](docs/ARCHITECTURE.md) · [Database](docs/DATABASE.md) · [Pipeline](docs/PIPELINE.md) ·
[Email engine](docs/EMAIL_ENGINE.md) · [AI tools](docs/AI_TOOLS.md) · [Design system](docs/DESIGN_SYSTEM.md) ·
[Security](docs/SECURITY.md)

## Repository

```
apps/web                 Next.js app (BFF proxy /api/v1/*, Better Auth /api/auth/*)
apps/api                 FastAPI app, workers, Alembic migrations, tests
packages/design-system   Tokens, theme, primitives
packages/schemas         OpenAPI-generated types shared with the API
packages/shared          Framework-free helpers (formatting, filters, shortcuts)
services/                Optional Railway services (email verifier, maps scraper)
docs/                    Architecture and engine documentation
```

## Local development

Requirements: Node ≥ 20.9 with pnpm 10, Python 3.12 with [uv](https://docs.astral.sh/uv/), Postgres 16+.

```bash
pnpm install
cd apps/api && uv sync && cp .env.example .env   # set DATABASE_URL, optionally GEMINI_API_KEY
uv run alembic -c alembic.ini upgrade head
uv run uvicorn scout.main:app --reload --port 8000            # API + in-process workers

cd ../web && cp .env.example .env.local                        # DATABASE_URL, BETTER_AUTH_SECRET, SCOUT_API_URL, INTERNAL_API_SECRET
pnpm dev                                                       # http://localhost:3000
```

Sign up, then use **Load demo data** on the Discover page (development only) to explore a seeded list.
Without `GEMINI_API_KEY` the API runs with deterministic parsing/planning (`AI_PROVIDER=local`).

Checks:

```bash
pnpm typecheck && pnpm lint && pnpm test           # web + packages
cd apps/api && uv run ruff check scout tests && uv run mypy scout && uv run pytest -q
pnpm schemas:generate                              # refresh shared API types after API changes
```

## Deployment

### 1. Neon (database)

Create a Neon project (or use the Vercel ↔ Neon integration). Two URLs are needed:

* **pooled** (`…-pooler…`) — runtime queries from the web (Better Auth) and the API
* **direct** (no `-pooler`) — Alembic migrations and the job queue's LISTEN/NOTIFY

### 2. Railway (API + workers)

Create a service from this GitHub repository with **Root Directory `apps/api`** — it builds
`apps/api/Dockerfile`, runs `alembic upgrade head` as the pre-deploy command and health-checks `/v1/health`
(see `apps/api/railway.json`). Environment:

| Variable | Value |
|---|---|
| `APP_ENV` | `production` |
| `DATABASE_URL` | Neon **pooled** URL |
| `DATABASE_DIRECT_URL` | Neon **direct** URL |
| `INTERNAL_API_SECRET` | same random 32+ byte secret as the web app |
| `CORS_ORIGINS` | the web origin, e.g. `https://scout-….vercel.app` |
| `WEB_APP_URL` | the web origin |
| `GEMINI_API_KEY` | Gemini key (optional; deterministic mode without it) |
| `GITHUB_TOKEN` | optional, raises GitHub API budget for email-pattern evidence |
| `VERIFIER_SERVICE_URL` | optional, `http://email-verifier.railway.internal:8080` |
| `GMAPS_SCRAPER_URL` | optional, `http://maps-scraper.railway.internal:8080` |
| `SMTP_ENABLED`, `SMTP_HELO_DOMAIN`, `SMTP_FROM_ADDRESS` | only where outbound port 25 is open and the HELO domain is yours with matching rDNS |
| `SEARXNG_URL` | optional, `http://searxng.railway.internal:8080` — turns on the free search pass before any Gemini grounding |
| `SEARCH_PROVIDERS` / `SEARCH_LOOKUP_PROVIDERS` | optional, defaults `searxng,duckduckgo` / `searxng` |
| `GEMINI_SEARCH_FALLBACK_ONLY` | optional, default `true` (`false` = Gemini grounding without the free search pass) |
| `SCOUT_EXTRAS` | optional build variable: `scraping` installs the Scrapling fetch tiers (~330 MB) |

Railway's Hobby plan blocks outbound SMTP (port 25): the email engine's health monitor detects it and
runs the fast path (domain intelligence, learned conventions, MX) without SMTP probes and without false
`INVALID` verdicts. Optional services live in `services/*/` with their own `railway.json`; use Railway
private networking between services.

**SearXNG (recommended):** add a second service from the same repository with Root Directory
`services/searxng`, name it `searxng`, do not generate a public domain, set `SEARXNG_SECRET` (random,
e.g. `openssl rand -hex 32`; SearXNG refuses to start without it), then set
`SEARXNG_URL=http://searxng.railway.internal:8080` on the API service. 0.5 vCPU / 512 MB is enough.

### 3. Vercel (web)

Import the repository with **Root Directory `apps/web`** (framework: Next.js; pnpm workspace detected).
Environment:

| Variable | Value |
|---|---|
| `DATABASE_URL` | Neon **pooled** URL (Better Auth tables are created by the API migration) |
| `BETTER_AUTH_SECRET` | random 32+ byte secret |
| `BETTER_AUTH_URL` | optional — defaults to the Vercel production / branch URL |
| `SCOUT_API_URL` | the Railway API URL, e.g. `https://scout-api.up.railway.app` |
| `INTERNAL_API_SECRET` | same value as the API |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | optional Google sign-in |

The browser only talks to the Next.js app; the BFF proxy (`/api/v1/*`) authenticates the session and
calls the API with a 60-second service JWT (audience `scout-api`) plus the workspace id.

## Cost envelope

Designed for ≈ 3,000 qualified leads/month within ~$30–40: Vercel hobby/pro, Neon free/launch, one
Railway service, Gemini Flash-class models used only where deterministic code cannot decide, no paid
enrichment APIs or proxies. Per-workspace budgets and hard caps are enforced by the API.
