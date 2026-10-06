# Scout — Database

PostgreSQL (Neon in production, Postgres 16 locally) is the canonical store for every entity,
campaign, job and event. The schema is defined by SQLAlchemy 2 models in
`apps/api/scout/db/models.py` and migrated with **Alembic** (`apps/api/migrations/`).
Production schema is never mutated by hand.

Conventions

* Primary keys: `uuid` (UUIDv7 generated in Python → time-ordered, index friendly).
  Event/log tables use `bigserial` for cheap monotonic cursors.
* Every business table carries `workspace_id` (FK, `ON DELETE CASCADE`) and every query is
  scoped by it. Global, non-personal caches (`domain_email_patterns`, `domain_dns_cache`,
  `sources`) are deliberately workspace-independent.
* Enumerations are `varchar` + `CHECK` constraints generated from Python enums
  (`scout/db/enums.py`) — easy to evolve, exported to TypeScript via OpenAPI.
* Timestamps are `timestamptz`, default `now()`.
* `jsonb` is used only where the shape is genuinely variable: raw source payloads, typed
  documents validated by Pydantic (campaign definition snapshot, enrichment plan, saved view
  layout, AI tool arguments), observation values, and event payloads.
* Extensions: `pg_trgm` (fuzzy names, global search), `unaccent` (name normalization in SQL),
  `citext` not used (we normalize in application code and store `normalized_*` columns).

---

## 1. Identity & tenancy

| Table | Purpose / key columns |
|---|---|
| `users` | Better Auth user (`id text` PK, `name`, `email` unique, `email_verified`, `image`, timestamps). Field names mapped to snake_case in Better Auth config. |
| `auth_sessions` | Better Auth sessions (`token` unique, `expires_at`, `user_id` FK, ip/user agent). |
| `auth_accounts` | Better Auth credential/OAuth accounts (`provider_id`, `account_id`, hashed `password`, tokens). |
| `auth_verifications` | Better Auth verification tokens. |
| `workspaces` | `id`, `name`, `slug` unique, `monthly_budget_usd`, `hard_budget_cap` bool, `settings jsonb` (freshness overrides, default exclusion, density…). |
| `workspace_members` | PK `(workspace_id, user_id)`, `role` ∈ owner/admin/member. |

## 2. Organization

| Table | Key columns | Constraints / indexes |
|---|---|---|
| `lists` | `name`, `description`, `entity_type` (person/company), `color`, `is_archived`, `archived_at`, `source_campaign_id`, `filter_snapshot jsonb` (when created from filters), `created_by` | unique `(workspace_id, lower(name))` where not archived |
| `list_memberships` | `list_id`, `person_id` / `company_id` (exactly one, CHECK), `added_at`, `added_by`, `added_via` (campaign/manual/ai/import/filter), `campaign_id` | unique `(list_id, person_id)` and `(list_id, company_id)` partial; index `(workspace_id, person_id)`, `(workspace_id, company_id)` |
| `saved_views` | `list_id` nullable (null = global People/Companies), `entity_type`, `name`, `filters jsonb` (FilterGroup tree), `sort jsonb`, `column_order`, `column_visibility`, `column_widths`, `pinned_columns`, `density`, `is_default`, `position` | unique `(workspace_id, list_id, lower(name))` |

Removing a membership **never** touches `companies`, `people`, discovery events or exposures.

## 3. Campaigns

| Table | Key columns |
|---|---|
| `campaigns` | `name`, `prompt`, `status` (draft/planning/running/paused/completed/exhausted/budget_reached/limit_reached/cancelled/failed), `stop_reason`, `definition jsonb` (validated `CampaignDefinition` snapshot, versioned), `definition_hash`, `target_qualified_count`, `target_list_id`, `mode` (people/companies), `seed_type` (search/list/import/selection/domains), `seed_ref jsonb`, `max_cost_usd`, `max_raw_candidates`, `template_id`, `parent_campaign_id`, `recurrence` (null; reserved for weekly/monthly), `created_by`, `started_at`, `paused_at`, `stopped_at`, `last_progress_at` |
| `campaign_filters` | normalized criteria: `scope` (company/person/website/email/score), `field`, `operator`, `value jsonb`, `condition_kind` (exact/semantic/numeric/geo/category), `is_required`, `position` |
| `campaign_sources` | `source_key`, `priority`, `status` (pending/active/exhausted/unhealthy/disabled), `query_plan jsonb`, `cursor jsonb` (resumable pagination), counters `raw_count`, `unique_count`, `qualified_count`, `error_count`, `last_run_at`, `last_error` |
| `campaign_exclusions` | `mode` (NONE, EXCLUDE_PREVIOUS_PEOPLE, EXCLUDE_PREVIOUS_COMPANIES, EXCLUDE_PREVIOUS_PEOPLE_AND_COMPANIES, EXCLUDE_EXPORTED, EXCLUDE_SPECIFIC_LISTS, EXCLUDE_CURRENT_LIST, EXCLUDE_CONTACTED, EXCLUDE_WITHIN_COOLDOWN, CUSTOM), `entity` (person/company), `exposure_types text[]`, `within_days`, `list_ids uuid[]`, `campaign_ids uuid[]`, `import_ids uuid[]` |
| `campaign_stats` | 1:1 counters: `raw_discovered`, `unique_new_companies`, `duplicates`, `excluded_previous`, `suppressed`, `reserved_elsewhere`, `companies_evaluated`, `companies_matched`, `people_found`, `emails_found`, `emails_safe`, `emails_accepted`, `qualified`, `rejected`, `errors`, `cost_usd`, `updated_at` — updated with atomic `x = x + n` |
| `campaign_reservations` | `campaign_id`, `entity_type`, `entity_key` (normalized domain or person identity key), `company_id`, `person_id`, `status` (reserved/qualified/released/expired), `reserved_at`, `expires_at` — **partial unique** `(workspace_id, entity_type, entity_key) WHERE status = 'reserved'` |
| `campaign_templates` | `name`, `definition jsonb`, `exclusion_mode`, `enrichment_plan jsonb` (columns to create), `last_run_at` |

## 4. Global lead registry

### 4.1 Companies

`companies`: `name`, `normalized_name`, `domain`, `normalized_domain` (registrable domain via
PSL, lowercase, IDNA), `website_url`, `description`, `country` (ISO-2), `region`, `city`,
`postal_code`, `address`, `latitude`, `longitude`, `industry`, `sub_industry`, `category_raw`,
`employee_min`, `employee_max`, `employee_confidence`, `phone`, `registry_source`
(e.g. `fr_sirene`), `registry_id` (e.g. SIREN), `status` (active/closed/unknown),
`company_confidence`, `has_conflicts`, `needs_review`, `website_status` (unknown/ok/unreachable/parked/redirected),
discovery-history columns (below), `last_crawled_at`, `last_enriched_at`, `last_tech_scan_at` (last technology
scan, also when nothing was detected — zero-tech sites are not re-scanned within the freshness window), timestamps.

Unique: `(workspace_id, normalized_domain)` where not null;
`(workspace_id, registry_source, registry_id)` where not null.
Indexes: GIN trigram on `normalized_name`; `(workspace_id, country, industry)`;
`(workspace_id, employee_min, employee_max)`.

`company_field_observations`: `company_id`, `field_name`, `value_json`, `source_type`
(website/registry/maps/directory/search_snippet/grounded_search/ai_extraction/import/user/tech_scan),
`source_key` (FK `sources.key`), `source_url`, `page_id` (FK `website_pages`), `evidence`,
`confidence`, `is_user_confirmed`, `is_current`, `observed_at`.
The canonical column on `companies` is chosen by the **field resolver**: user-confirmed >
source quality × confidence × freshness decay, with agreement bonus. Disagreeing credible
observations set `has_conflicts`, surfacing in *Needs review*.

`company_discovery_events`: one row per (campaign, candidate): `source_key`,
`source_entity_id`, `candidate_key` (normalized domain, else `name|city`), raw canonical
structure (`name`, `website`, `domain`, `location`, `category`, `source_url`, `raw_data jsonb`),
`company_id`, `stage` (checkpoint of the per-company pipeline), `stage_data jsonb`,
`outcome` (pending/qualified/rejected/duplicate/excluded_previous/suppressed/reserved_elsewhere/error),
`reason`, `observed_at`. Unique `(campaign_id, candidate_key)` — the in-campaign dedupe.

### 4.2 People

`people`: `company_id`, `first_name`, `last_name`, `full_name`, `normalized_name`
(lowercase, unaccented, collapsed whitespace), `job_title` (original, never overwritten by
normalization), `normalized_title`, `department`, `seniority`, `role_family`,
`decision_power` (0–100), `public_profile_url`, `location`, `identity_confidence`,
`primary_email_id`, `needs_review`, `has_conflicts`, discovery-history columns,
`last_verified_at`, timestamps.

Unique: `(workspace_id, company_id, normalized_name)`;
`(workspace_id, public_profile_url)` where not null. Trigram index on `normalized_name`.
No global name-only dedupe.

`person_employments` (employment history, architected now): `person_id`, `company_id`,
`title`, `is_current`, `started_at`, `ended_at`, `source_key`, `observed_at`.

`person_field_observations`: same shape as company observations.
**No person row is created without at least one evidence observation** (enforced in
`registry.upsert_person`, which requires an `Evidence` argument).

`person_discovery_events`: per (campaign, person) outcome and reason.

### 4.3 Discovery-history columns (on both `companies` and `people`)

`first_seen_at`, `last_seen_at`, `last_enriched_at`, `first_campaign_id`, `last_campaign_id`,
`times_discovered`, `times_exported`, `last_exported_at`, `times_added_to_lists`,
`contacted_at`, `suppressed_at`.

`lead_discovery_history` is a **view** unioning company and person discovery events with
their campaign names, answering “when did I first scrape this company?” and
“which campaigns produced this person?”.

### 4.4 Lead exposures — “has the user already seen this?”

`lead_exposures`: `id bigserial`, `workspace_id`, `entity_type` (person/company), `entity_id`,
`company_id` (denormalized for person rows), `exposure_type`
(DISCOVERED/SHOWN/ADDED_TO_LIST/IMPORTED/EXPORTED/CONTACTED/ENRICHED), `campaign_id`,
`list_id`, `import_id`, `export_id`, `occurred_at`.

Index: `(workspace_id, entity_type, entity_id, exposure_type, occurred_at)` and
`(workspace_id, list_id)`. Rows are append-only. When a person exposure is written, a company
exposure of the same type is written too, so company-level exclusion is a single lookup.

`DISCOVERED` means *delivered to the user as a qualified lead* (or company result). Candidates
rejected internally are recorded in `*_discovery_events` but are not exposures — the user
never saw them, and their cached data is reused at no cost if they qualify later.

### 4.5 Suppression

`suppression_list`: `entity_type` (person/company/email/domain), `entity_id` nullable,
`value` (normalized email or domain or identity key), `reason`
(user_request/opt_out/do_not_contact/manual/invalid/gdpr), `note`, `created_by`, `created_at`.
Unique `(workspace_id, entity_type, value)`. Checked before every other rule; suppressed
entities are never silently re-added by any code path (campaign, import, AI tool).

## 5. Contact data

`emails`: `person_id` (nullable for company-level role addresses), `company_id`, `address`
(lowercase), `local_part`, `domain`, `kind` (person/role/generic), `discovery_method`
(published/known_pattern/inferred_pattern/permutation/import/user), `pattern`, `source_url`,
`status` (SAFE/RISKY/CATCH_ALL/UNKNOWN/INVALID), `mx_valid`, `smtp_result`
(accepted/rejected/unknown/timeout/blocked/not_attempted), `catch_all`, `disposable`,
`role_address`, `free_provider`, `pattern_confidence`, `overall_confidence`, `is_primary`,
`last_checked_at`. Unique `(workspace_id, address)`.

`email_checks`: append-only history per email (`verifier`, raw `result jsonb`, derived
fields, `duration_ms`, `error`).

`domain_email_patterns` (global data asset): `domain`, `pattern` (`{first}.{last}` …),
`confidence`, `supporting_samples`, `successful_checks`, `failed_checks`, `last_verified_at`.
Unique `(domain, pattern)`.

`domain_dns_cache` (global): `domain` PK, `has_mx`, `mx_hosts jsonb`, `has_a`, `null_mx` (RFC 7505), `catch_all`,
`catch_all_checked_at`, `checked_at`, `error`.

## 6. Website cache

`website_crawl_runs`: `company_id`, `domain`, `status`, `tier_max` (http/crawl4ai/browser),
`pages_fetched`, `pages_failed`, `bytes`, `error_category`, `error`, `started_at`, `finished_at`, `job_id`.

`website_pages`: `company_id`, `url`, `canonical_url`, `page_type`
(home/about/team/services/solutions/contact/pricing/careers/blog/news/legal/case_studies/other),
`title`, `meta_description`, `content_text` (cleaned, capped at 40 kB), `content_hash`
(sha256 of normalized text), `status_code`, `fetched_at`, `etag`, `last_modified`,
`content_type`, `language`, `fetch_tier`, `head_html` (home page only, ≤ 48 kB, for tech
fingerprints), `response_headers jsonb` (home page only), `links jsonb` (social + internal
priority links), `emails jsonb`, `phones jsonb`, `structured_data jsonb` (JSON-LD),
`word_count`, `crawl_run_id`. Unique `(company_id, canonical_url)`.

## 7. Dynamic enrichment

`custom_columns`: `list_id` nullable, `name`, `slug`, `data_type`
(boolean/text/number/url/email/enum/json/date), `kind` (factual/generated), `entity_type`
(company/person), `resolver_type` (deterministic/cached_website/website_recrawl/
tech_detection/public_source/web_search/ai_on_cached_content/ai_web_research/composite),
`instructions`, `configuration jsonb` (the EnrichmentPlan), `source_preferences jsonb`,
`confidence_threshold`, `refresh_policy jsonb` (`{refresh_days}`), `depends_on uuid[]`,
`position`, `is_hidden`, `created_by`, timestamps. Unique `(workspace_id, list_id, slug)`.

`custom_field_values`: `column_id`, `entity_type`, `entity_id`, `value_json`,
`display_value text` (for sort/filter), `confidence`, `source_id` (`sources.key`),
`source_url`, `evidence`, `resolver`, `status`
(not_started/queued/running/success/unknown/failed/stale), `error`, `input_hash`
(skip recomputation when unchanged), `is_user_override`, `model`, `cost_usd`,
`observed_at`, `updated_at`. **Unique `(column_id, entity_type, entity_id)`**; index
`(column_id, display_value)`.

## 8. Signals, technology, scores

`technologies`: `company_id`, `name`, `category`, `version`, `confidence`, `detector`,
`source_url`, `observed_at`. Unique `(company_id, name)`.

`signals`: `company_id`, `person_id`, `type` (hiring/job_opening/funding/product_launch/
website_update/new_executive/new_location/press/new_technology/expansion), `value jsonb`,
`source_type`, `source_url`, `evidence`, `confidence`, `observed_at`.

`qualification_scores`: `campaign_id` (nullable), `company_id`, `person_id`, `icp_score`,
component scores (`company_fit`, `person_fit`, `intent`, `contactability`, `evidence_score`),
confidences (`company_confidence`, `person_confidence`, `email_confidence`,
`enrichment_confidence`, `overall_confidence`), `qualified`, `gate_results jsonb`
(each gate: passed, reason), `weights jsonb`, `explanation jsonb` (evidence bullets),
`computed_at`. Unique `(campaign_id, company_id, person_id)` (nulls not distinct).

## 9. Sources

`sources` (global): `key` unique (e.g. `fr_registry`, `google_maps`, `web_search_ddg`,
`gemini_search`, `osm`, `yc`, `hn`, `github`, `website`, `import`, `user`), `name`, `kind`
(discovery/evidence/both), `quality_score` (0–1), `enabled`, `priority`, health counters
(`requests`, `successes`, `failures`, `blocks`, `results`, `duplicates`, `qualified`,
`avg_latency_ms`), `unhealthy_until`, `last_error`, `last_success_at`.
Source quality feeds field confidence (official website/registry ≈ 0.95, public profile
≈ 0.85, reputable directory ≈ 0.75, search snippet ≈ 0.6, unverified aggregation ≈ 0.4,
user ≈ 1.0).

## 10. Jobs & events

`jobs`: `type`, `status` (pending/claimed/running/completed/failed/retrying/cancelled/
paused/dead_letter), `priority`, `payload jsonb`, `result jsonb`, `dedupe_key`, `attempts`,
`max_attempts`, `run_after`, `lease_expires_at`, `locked_by`, `heartbeat_at`,
`last_error`, `error_category`, `parent_job_id`, `blocked_by_count` (dependency graph),
`campaign_id`, timestamps.
Indexes: claim index `(priority DESC, run_after) WHERE status IN ('pending','retrying')`;
lease reaper `(lease_expires_at) WHERE status IN ('claimed','running')`;
unique `(workspace_id, dedupe_key) WHERE status NOT IN ('completed','failed','cancelled','dead_letter')`
for idempotent enqueue.

`job_dependencies`: `(job_id, depends_on_job_id)` — a job becomes claimable when
`blocked_by_count = 0`.

`job_attempts`: per attempt `worker_id`, `started_at`, `finished_at`, `status`, `error`,
`error_category`, `duration_ms`.

`job_events` (`bigserial`): `workspace_id`, `job_id`, `campaign_id`, `type`
(`campaign.progress`, `campaign.status`, `lead.qualified`, `cell.updated`, `column.progress`,
`job.failed`, `import.progress`, …), `payload jsonb`, `created_at`. This is the SSE log.

## 11. Chat, audit, usage, IO

`chat_threads` (`list_id`, `title`), `chat_messages` (`role`, `content`, `parts jsonb`
(text + tool cards), `context jsonb` (UI context snapshot), `model`, `tokens_in`,
`tokens_out`), `assistant_actions` (`tool_name`, `arguments jsonb`, `status`
(executed/failed/awaiting_confirmation/rejected/undone), `result jsonb`, `error`,
`requires_confirmation`, `undo_payload jsonb`, `confirmed_at`, `executed_at`).

`audit_logs`: `actor_type` (user/assistant/system), `actor_id`, `action`, `entity_type`,
`entity_ids uuid[]`, `campaign_id`, `assistant_action_id`, `payload jsonb`,
`undo_payload jsonb`, `undone_at`, `created_at`.

`usage_events`: `campaign_id`, `job_id`, `category` (ai_tokens/grounded_search/
crawl_request/browser_request/verification_request/maps_request/registry_request/
web_search), `resolver`, `model`, `source_key`, `quantity`, `tokens_in`, `tokens_out`,
`estimated_cost_usd`, `created_at`. Indexed by `(workspace_id, created_at)` and
`(campaign_id)`.

`imports`: `list_id`, `filename`, `status`, `column_mapping jsonb`, `mark_as_known`
(default true), counters, `errors jsonb`. `exports`: `list_id`, `view_id`, `scope`
(selected/view/list), `columns_mode` (visible/all), `format` (csv/json), `row_count`.

`webhooks`: `url`, `secret`, `events text[]`, `is_active` — CRM-ready outbound events.

## 12. Index inventory (performance)

* `workspace_id` leading column on every business index.
* `companies(workspace_id, normalized_domain)` unique, trigram `normalized_name`.
* `people(workspace_id, company_id, normalized_name)` unique, trigram `normalized_name`.
* `emails(workspace_id, address)` unique, `(address)` (global pattern-memory lookups), `(person_id)`, `(workspace_id, status)`.
* `grounded_research(workspace_id, cache_key, created_at)` for the research cache lookup.
* `list_memberships(list_id, added_at, id)` for cursor pagination.
* `campaigns(workspace_id, status)`.
* Job queue partial indexes (above).
* `custom_field_values(column_id, display_value)`, `(entity_type, entity_id)`.
* `lead_exposures(workspace_id, entity_type, entity_id, exposure_type, occurred_at)`.
* `suppression_list(workspace_id, entity_type, value)` unique.

Pagination is keyset (`(sort_value, id) > (…)`), never `OFFSET`.
