# Research — Pipeline, Job Queue & Enrichment Engine

## 1. Job queue (Postgres, no Redis)

Implementation: `apps/api/scout/jobs/`.

* **Enqueue** — `queue.enqueue(type, payload, workspace_id, campaign_id=None, priority=0,
  dedupe_key=None, run_after=None, depends_on=[])`. With a `dedupe_key`, enqueue is
  idempotent (`ON CONFLICT DO NOTHING` on the active-job partial unique index). Inserting
  issues `NOTIFY scout_jobs`.
* **Claim** —
  ```sql
  UPDATE jobs SET status='claimed', locked_by=:worker, attempts=attempts+1,
         lease_expires_at=now()+:lease, heartbeat_at=now(), updated_at=now()
  WHERE id IN (
    SELECT j.id FROM jobs j
    LEFT JOIN campaigns c ON c.id = j.campaign_id
    WHERE j.status IN ('pending','retrying') AND j.run_after <= now()
      AND j.blocked_by_count = 0
      AND (c.id IS NULL OR c.status IN ('planning','running'))
      AND j.type = ANY(:types)
    ORDER BY j.priority DESC, j.run_after
    LIMIT :n FOR UPDATE OF j SKIP LOCKED)
  RETURNING *;
  ```
* **Run** — status `running`, a `job_attempts` row is opened; a heartbeat task extends the
  lease every `lease/3`. Handlers are registered with `@job_handler("type", pool="crawl")`.
* **Finish** — `completed` (+ result, dependants' `blocked_by_count` decremented) or error
  handling: `RetryableError` → `retrying` with `run_after = now() + backoff(attempt)` where
  `backoff = min(cap, base·2^attempt) · U(0.5, 1.0)` (full jitter); after `max_attempts` →
  `dead_letter`. `PermanentError` → `failed`. `BlockedError` (403/429 anti-bot) gets at most
  2 retries with long delays. Error category and message are persisted on the job and attempt.
* **Reaper** — every 15 s: `claimed`/`running` jobs with `lease_expires_at < now()` go back to
  `retrying` (attempt is counted) → a crashed worker never loses work.
* **Pause/resume/cancel** — campaign pause flips its `pending`/`retrying` jobs to `paused`;
  running handlers check the campaign status between stages (cooperative) and requeue
  themselves as `paused`. Resume flips `paused → pending`. Cancel → `cancelled`.
* **Idempotency** — every handler is safe to re-run: per-company processing checkpoints its
  stage in `company_discovery_events.stage/stage_data`; upserts use natural unique keys;
  counters are only incremented on state transitions guarded by `WHERE outcome = 'pending'`.
* **Concurrency pools** — one asyncio semaphore per integration, configured independently
  (`POOL_HTTP=24`, `POOL_BROWSER=2`, `POOL_MAPS=1`, `POOL_GEMINI=6`, `POOL_SEARCH=2`,
  `POOL_SMTP=4`, `POOL_PUBLIC_API=4`) plus per-domain politeness (max 2 concurrent, ≥ 400 ms
  spacing) so no integration can starve the worker.
* **Workers** run inside the API process (`WORKER_ENABLED=true`, `WORKER_SLOTS=16`) or as
  a dedicated process (`python -m scout.worker`) — same code, no rewrite needed to scale out.
  If benchmarks ever justify Redis, only `scout/jobs/queue.py` changes.

## 2. Campaign definition

`scout/schemas/campaign.py` — Pydantic model, exported to TypeScript and to the AI parser
as the structured-output schema.

```jsonc
{
  "version": 1,
  "mode": "people",                       // or "companies" (company-only search)
  "target_qualified_count": 3000,
  "company_filters": {
    "industries": ["marketing agency"],   // free text; mapped to NAF codes / Maps categories / keywords
    "keywords": [],                       // extra discovery keywords
    "countries": ["FR"], "regions": [], "cities": [],
    "employee_range": {"min": 2, "max": 30},
    "exclude_keywords": []
  },
  "website_conditions": [
    {"type": "keyword_any", "terms": ["instagram", "manychat"], "required": true},
    {"type": "semantic_service", "concept": "Instagram marketing services", "required": true}
  ],
  "people_filters": {
    "titles": ["Founder","Co-Founder","CEO","Owner"],
    "role_families": ["founder","executive"], "seniorities": [], "departments": [],
    "max_people_per_company": 1
  },
  "required_fields": ["company","person","professional_email"],
  "minimum_email_confidence": 80,
  "minimum_person_confidence": 80,
  "minimum_icp_score": 75,
  "accepted_email_statuses": ["SAFE"],    // RISKY may be allowed explicitly
  "exclusion": {
    "mode": "EXCLUDE_PREVIOUS_PEOPLE",
    "previous_people": true, "previous_companies": false,
    "allow_new_people_at_existing_companies": true,
    "list_ids": [], "cooldown_days": null, "rules": []
  },
  "enrichments": [],                      // custom columns to create & require
  "signals": [],                          // optional intent signals to collect
  "score_weights": {"company_fit":35,"person_fit":20,"intent":20,"contactability":15,"evidence":10},
  "sources": {"preferred": [], "excluded": []},
  "limits": {"max_cost_usd": null, "max_raw_candidates": 60000, "max_runtime_hours": 72},
  "seed": {"type": "search"}              // or list / import / selection / domains
}
```

Exact vs semantic conditions (spec §104) are distinguished by the parser: “mentions /
contains / uses the word” → `keyword_any`/`keyword_all`/`regex` (deterministic);
“actually offers / sells / specializes in” → `semantic_service` / `semantic_classification`
(AI with evidence). Tech names (“uses Shopify”) → `technology`.

## 3. The funnel

```
PROMPT → ICP PARSER → CAMPAIGN PLAN → EXCLUSION PLAN → SOURCE ROUTER
  → RAW COMPANY DISCOVERY → CANONICALIZATION → GLOBAL REGISTRY CHECK → DUPLICATE CHECK
  → CHEAP PREQUALIFICATION → WEBSITE CRAWL → CUSTOM REQUIRED CONDITIONS
  → COMPANY QUALIFICATION → DECISION MAKER RESOLUTION → GLOBAL PERSON REGISTRY CHECK
  → EMAIL FINDING → EMAIL VERIFICATION → SIGNALS → SCORING → QUALITY GATE
  → QUALIFIED LEAD → LIST
```

### 3.1 `campaign.plan`
* Validate definition, compile exclusion rules (§5), resolve target list (create
  “<Campaign name>” list if none), choose sources via the router, persist
  `campaign_sources` with query plans, set status `running`, enqueue one `campaign.discover`
  per active source and the first `campaign.tick`.

### 3.2 `campaign.discover` (per source, resumable)
1. Read `cursor`, call `adapter.discover(query, cursor)` → `DiscoveryPage(candidates,
   next_cursor, exhausted)` under the source's pool. Record health metrics.
2. **Canonicalize** each `RawCandidate`: registrable domain (PSL, IDNA, strip `www`,
   reject social/marketplace/directory hosts), normalized name (lowercase, unaccent, legal
   suffix removal: SAS, SARL, SASU, EURL, GmbH, Ltd, Inc, LLC, …), country, city.
3. **In-campaign dedupe**: insert into `company_discovery_events` with
   `ON CONFLICT (campaign_id, candidate_key) DO NOTHING` → duplicates counted.
4. **Registry check (batch SQL)**: match existing companies by domain, registry id, or (only
   when neither exists) trigram name+city ≥ 0.92 in the same city. Matched candidates reuse the
   canonical company (no duplicate rows, cached crawl/enrichment reused).
5. **Suppression + company-level exclusion rules (batch SQL)** → `excluded_previous` /
   `suppressed`; dropped immediately, zero crawl/AI/SMTP spend.
6. **Cheap prequalification** from source data only: country, explicit employee band
   (registry), category/NAF match, exclude keywords, closed businesses. No network.
7. Survivors: upsert company (+ source observations) and enqueue `company.process`
   (dedupe key `campaign:company`). The discover job only pulls the next page when the
   in-flight pipeline is below `need × 1.5 / estimated_yield` (back-pressure), else it
   reschedules itself.

### 3.3 `company.process` (domain-centric, checkpointed stages)

| Stage | Work | Cost class | Drop reason |
|---|---|---|---|
| `reserve` | `campaign_reservations` hold on the company key (company-mode campaigns) | free | `reserved_elsewhere` |
| `resolve_website` | when the source has no website (registry/maps): candidate domains from name → DNS → fetch home → verify name/SIREN/phone/address on page; search fallback | cheap | `no_website` (company-only campaigns may continue) |
| `crawl` | reuse `website_pages` if fresh (≤ 30 d) else tiered crawl (§4) | cheap | `website_unreachable`, `parked_domain` |
| `website_conditions` | keyword/regex on cached text (deterministic) → then semantic conditions on relevant chunks (AI). Results cached by `(company, condition_hash, content_hash)` | free → AI | `condition_failed:<name>` |
| `company_qualification` | industry fit (category/NAF/text), size (registry band, team-page count, about-page statements), country; company confidence | free | `company_fit_below_threshold` |
| `people` | (people mode) extract decision makers from registry directors, JSON-LD, team/about pages, legal notices (“directeur de la publication”), AI on ambiguous team pages (names must appear verbatim), grounded search fallback (sources required) | free → AI → search | `no_decision_maker` |
| `person_registry` | per person: reservation (atomic) → suppression → person-level exclusion rules → choose best N by role fit/decision power/confidence | free | `excluded_previous_person` |
| `email` | published → known domain pattern → inferred pattern → ≤ 6 ranked permutations | free | `no_email_candidate` |
| `verify` | MX (cached 30 d) → SMTP (if enabled) → catch-all probe (cached per domain) → status | cheap | `email_not_accepted` |
| `signals` | only if requested (hiring/careers pages, website freshness, tech changes) | free | — |
| `enrichments` | required custom columns for this campaign (same engine as §6) | varies | `enrichment_condition_failed` |
| `score` | ICP score + confidences (§3.5) | free | — |
| `gate` | quality gate (§3.6) | free | first failing gate |
| `deliver` | single transaction: upsert person/emails/observations, reservation → `qualified`, exposures (`DISCOVERED` person+company, `ADDED_TO_LIST`), list membership, history counters, `campaign_stats.qualified += 1`, `job_events lead.qualified` | free | — |

Between stages the handler checks campaign status (pause/cancel) and budget.
Rejected candidates keep their reason on `company_discovery_events.outcome/reason` (visible
in the campaign's “Rejected” view: *why a lead was rejected* is never hidden).

### 3.4 Adaptive discovery (`campaign.tick`, every ~20 s while running)
* Funnel counts: raw → unique new → company matched → person found → email found → email
  accepted → qualified. `yield = qualified / processed_candidates` (with a Bayesian prior of
  15 % until 50 candidates are processed).
* `needed_raw = (target − qualified − in_flight × yield) / yield`; ensure enough
  discover jobs are scheduled; when a source exhausts its query plan, the router expands
  queries (more cities, synonyms, adjacent categories, more registry departments).
* Stop conditions → terminal status with explicit reason: target reached; all eligible sources
  exhausted; campaign max cost or workspace hard cap reached; safety limits
  (`max_raw_candidates`, `max_runtime_hours`); user stop.
* ETA appears only after ≥ 20 qualified leads in the last 30 min (rolling rate); no fake ETA.
* Quality is never relaxed automatically. When sources run dry the campaign reports
  “2,631 / 3,000 qualified · sources becoming exhausted · Broaden criteria?” and the user decides.

### 3.5 ICP score (0–100)
Default weights: company fit 35, person fit 20, intent 20, contactability 15, evidence 10
(campaign may override). Components that were not requested/collected (e.g. intent when no
signal stage ran) are excluded from the denominator rather than scored as zero, so a lead
is not penalized for data we chose not to collect; the explanation states it.

### 3.6 Quality gate (all must pass → `qualified = true`)
company fit ≥ threshold · person identified (people mode) · role accepted ·
person confidence ≥ `minimum_person_confidence` · email exists when required ·
email status ∈ `accepted_email_statuses` and confidence ≥ `minimum_email_confidence` ·
no exclusion conflict · not suppressed · required custom conditions pass ·
ICP score ≥ `minimum_icp_score`. Only qualified rows count toward the target.

## 4. Crawler (tiered)

1. **httpx** (L1) through an SSRF-safe network backend (validates resolved IPs at connect
   time; re-validates every redirect hop; http/https only; ports 80/443/8080/8443).
   robots.txt honoured (cached per domain), conditional requests with ETag/Last-Modified.
2. **selectolax** (L2) → cleaned text (scripts/styles removed, boilerplate collapsed), title,
   meta description, language, links, mailto/tel, JSON-LD, headings, social profiles.
3. **Crawl4AI** (L3, optional extra) when a page looks JS-rendered (text < 300 chars with
   an app root, `<noscript>` hints) — markdown output reused as text.
4. **Playwright** (L4, optional extra) only if L3 is unavailable/fails.

Page selection: home → sitemap.xml (+ robots sitemaps) and on-page links scored against the
priority list (about, about-us, team, our-team, leadership, company, services, solutions,
contact, contact-us, pricing, careers, jobs, news, blog, case studies, and FR/DE/ES
equivalents: qui-sommes-nous, equipe, a-propos, mentions-legales, tarifs, recrutement…).
Budget 5–12 pages; whole-domain crawls are never performed. Content hashes skip
re-processing of unchanged pages.

## 5. Exclusion engine

`scout/services/exclusion.py`

```python
ExclusionRule(entity="person"|"company",
              exposure_types=[...]|None,     # None = any exposure
              within_days=None|int,
              list_ids=[], campaign_ids=[], import_ids=[],
              include_list_history=True)    # ADDED_TO_LIST exposures, not just current membership
```

| Mode | Compiled rules |
|---|---|
| NONE | — (registry reuse still applies: no duplicate entities, cached data reused) |
| EXCLUDE_PREVIOUS_PEOPLE | person: any exposure |
| EXCLUDE_PREVIOUS_COMPANIES | company: any exposure |
| EXCLUDE_PREVIOUS_PEOPLE_AND_COMPANIES | both |
| EXCLUDE_EXPORTED | person (and company if requested): `EXPORTED` |
| EXCLUDE_SPECIFIC_LISTS | person and/or company: membership (current + historical) in `list_ids` |
| EXCLUDE_CURRENT_LIST | as above with the target list |
| EXCLUDE_CONTACTED | person: `CONTACTED` |
| EXCLUDE_WITHIN_COOLDOWN | person: any exposure within N days |
| CUSTOM | explicit rule list |

`allow_new_people_at_existing_companies=true` simply means no company rule is compiled;
`exclude_existing_companies_entirely=true` adds the company rule. Defaults: requests
mentioning *new / fresh / not already scraped / never seen* → `EXCLUDE_PREVIOUS_PEOPLE`
(+ company rule only when explicitly requested). Imported contacts carry `IMPORTED`
exposures (when `mark_as_known`, default true), so they are excluded like any other.

Evaluation is batched SQL (`= ANY(:ids)`) at two points: after canonicalization (company
rules) and after person resolution (person rules), plus a final re-check inside the
`deliver` transaction (race-safe together with reservations).

## 6. Dynamic enrichment engine

`scout/enrich/`

### 6.1 Planner
Input: column name + natural-language instruction (+ optional explicit config).
Output: `EnrichmentPlan` (stored in `custom_columns.configuration`):

```jsonc
{ "name": "Offers ManyChat", "data_type": "boolean", "kind": "factual",
  "entity_type": "company", "resolver": "ai_on_cached_content",
  "strategy": "semantic_website_classifier",
  "concept": "offers ManyChat / chatbot automation as a client service",
  "keywords": ["manychat","chatbot","automation dm"],
  "input_sources": ["services","home","about"],
  "confidence_threshold": 0.8, "refresh_days": 30,
  "cost_class": "AI", "depends_on": [] }
```

Planning order (cheapest sufficiently reliable first, spec §94):
1. **Deterministic rules** (no AI): “mentions / contains X” → `keyword` on cached website;
   regex-shaped asks (phone, VAT, SIREN) → `regex`; social accounts (Instagram, LinkedIn,
   Facebook, TikTok, YouTube, X) → `website_extraction` from cached links; known technology
   names → `tech_detection`; canonical fields (city, size, email status…) → `deterministic`.
2. **AI planner** (`models.reasoning`, structured output) for everything else:
   semantic classification, extraction, research needing external sources
   (`ai_web_research`: job postings, podcasts, funding, press), generated copy
   (`kind=generated`: summary, outreach angle, opener).

Resolver cost classes: FREE (deterministic, cached website, keyword, regex, extraction),
CHEAP (website recrawl, tech detection), AI (AI on cached content), WEB_SEARCH (grounded
search), EXPENSIVE (browser recrawl + research). Grounded search is never used for a
keyword question.

### 6.2 Execution
* `enrichment.column` fans out `enrichment.batch` jobs (≤ 100 entities) for missing/stale
  cells (or all, for refresh). Cells move `queued → running → success|unknown|failed`.
* Entities sharing a company share the work (company-level columns are computed once per
  company, displayed on every person row).
* Dependencies: a column with `depends_on` creates its batches with job dependencies; e.g.
  *CEO email* = person resolution → email finder → verifier.
* **Relevant-chunk retrieval** for AI resolvers: cached pages → ~900-char passages →
  BM25-style scoring against concept + keyword expansions + page-type priors → top 6
  passages (≤ 6 k chars) → model. Inputs are hashed; unchanged inputs + unchanged plan ⇒ no
  model call (spec §57, §93).
* Outputs are schema-validated: `{value, status: "true"|"false"|"unknown"|…, confidence,
  evidence_quote, source_url}`. The evidence quote must appear in the cited passage; otherwise
  confidence is capped at 0.5 and status `unknown`. Generated fields are stored with
  `kind=generated` and never used as factual evidence.
* User overrides (`is_user_override`) are never overwritten automatically; a refresh asks
  for confirmation.

### 6.3 Freshness policy (`scout/services/freshness.py`, overridable per workspace)
website 30 d · company description 30 d · role 45 d · email 60 d · MX 30 d · email pattern
180 d (re-validated on failures) · technology 30 d · grounded research 14 d · custom column
per `refresh_days`. UI badges: Fresh (< 7 d) · 30d · 90d · Stale.

## 7. Website resolution for sources without URLs

Registry/maps candidates without websites: generate domain candidates from the normalized
name (`agencelumiere.fr`, `agence-lumiere.fr`, `.com`, without legal words), DNS A/AAAA check,
fetch home, verify identity on page (SIREN/SIRET in legal notice → 0.98, exact phone → 0.9,
name + city → 0.8). If unresolved, a single free web-search query (`"<name>" <city>`) and
then, only within budget, grounded search. Unresolved companies stay `website_status=unknown`.

## 8. Observability

`/v1/diagnostics` and the *Usage* screen expose: companies/min, pages/min, crawl success %,
browser fallback %, people found %, emails found %, SAFE %, catch-all %, qualification %,
duplicates %, previously-seen exclusions %, Gemini calls, grounded searches, token usage,
job failures by category, source failures, cost per qualified lead. Logs are structured JSON
(structlog) with `job_id`, `campaign_id`, `workspace_id`, `company_id`, `person_id`; secrets
are never logged.
