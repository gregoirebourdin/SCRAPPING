# Email Intelligence Engine

Scout finds and verifies professional emails **per domain, not per contact**. Everything that is
true about a domain (MX, provider, observed addresses, the company's email convention, catch-all
behaviour, how its mail server answers SMTP) is computed once, stored, and reused by every contact,
campaign and workspace, subject to freshness. SMTP is the expensive exception, not the rule.

Priority is absolute: **observed evidence > learned pattern > generated candidate**. No LLM ever
produces or guesses an address.

```
Person + Domain
  → cache / global registry (user-confirmed, fresh SAFE / LIKELY_SAFE)
  → Domain Intelligence Profile (built once per domain)
      MX · provider · website emails · public GitHub evidence* · RDAP · learned patterns · catch-all · SMTP facts
  → observed address for this person (affinity-guarded)
  → dominant learned pattern (1–2 renderings)
  → priors only when the domain has no confirmed convention          (≤ 3 candidates in total)
  → name-affinity guard → MX/DNS → confidence engine
  ─── FAST PATH ends here when the verdict is SAFE / LIKELY_SAFE, or when SMTP cannot settle it
      (catch-all domain, no MX, SMTP unavailable)
  → DEEP PATH (background, per domain): one SMTP session for every pending person of the domain
      + random catch-all probes → greylist/temporary answers retried (5 min, 30 min, 2 h)
      → if every first guess is rejected on a healthy, non catch-all server: ONE expansion round
  → final status, learning (pattern successes/failures, verified samples), deferred campaign delivery
```

\* GitHub commit addresses are **pattern evidence only**: GitHub's Acceptable Use Policy forbids
using them for unsolicited email, so they teach the domain convention but are never offered as a
contact's address.

## Statuses

| Status | Meaning |
|---|---|
| `SAFE` | Confirmed: published for this person on the company's own site (strong name affinity, valid MX), or accepted by a **healthy** SMTP probe on a domain proven **not** catch-all. |
| `LIKELY_SAFE` | Strong evidence without mailbox confirmation: the company's confirmed convention (real samples / SMTP successes), valid MX, strong name affinity, domain not known catch-all. Also: SMTP-proven mailbox with only a medium name match (`john@` could be another John). |
| `RISKY` | Plausible but not confirmed (moderate prior, weaker affinity, catch-all unknown after an accept). |
| `CATCH_ALL` | The domain accepts any address: a guessed address cannot be confirmed (never `SAFE` because SMTP accepted it). |
| `TEMPORARY_UNKNOWN` | The mail server said "try later" (4xx, 421, greylisting, rate limit, timeout). A retry is scheduled; never treated as invalid. |
| `UNKNOWN` | Not enough evidence. |
| `INVALID` | Mailbox rejected as unknown user (RFC 3463 `X.1.1`) by a healthy SMTP path on a non catch-all domain, no MX / null MX, disposable domain, or bad syntax. |

Campaign default for "professional email": `SAFE` + `LIKELY_SAFE`. "Strictly verified / SAFE only"
keeps `SAFE`; "risky OK" adds `RISKY`. `LIKELY_SAFE` is labelled distinctly in the UI.

## Components

| Module | Role |
|---|---|
| `scout/email/contracts.py` | Shared, I/O-free types: `DomainIntel`, `ObservedEmail`, `PatternStat`, `DomainProbeResult`, `RcptVerdict`, `SessionOutcome`, `Signal`, `Verdict`. |
| `scout/email/intel/` | Domain Intelligence Profiles: provider detection from MX, observed samples, pattern learning (recency + source weighted, smoothed), GitHub org commits (pattern evidence), RDAP, per-domain lock + TTL cache so 20 contacts trigger one build. |
| `scout/email/affinity.py` | Name-affinity guard (0–1). `marie@acme.com` never goes to John Smith; a colleague's address is rejected; `{first}@` is only convincing when it is the domain's convention and the first name is not shared. |
| `scout/email/confidence.py` | Explainable confidence: base probability = empirical precision of the resolver (Beta-smoothed around a default prior) or the learned pattern posterior; signals in log-odds (affinity, MX, SMTP when healthy, catch-all, free provider); status ladder. |
| `scout/email/stats.py` | Empirical scoring: attempts / confirmed correct / confirmed wrong / latency / cost per resolver, source, pattern, technique and provider. |
| `scout/email/engine.py` | Fast path (`resolve_for_person`), candidate generation (≤ 3), deep-path conclusion (`conclude_after_probe`), persistence + learning (`persist_deep_verdict`), deferred-delivery hooks. |
| `scout/email/smtp/` | SMTP layer: RFC 3463 classification, one batched session per domain, random catch-all probes, health monitor (`HEALTHY` / `DEGRADED` / `BLOCKED` / `UNKNOWN`, global + per provider), deterministic simulated mail world for tests and benchmarks. |
| `scout/email/deep.py` | Deep-path queue (`email_verification_requests`) and the `email.deep_domain` job: claims due requests of a domain, one probe, conclusions, retries with backoff. |
| `scout/services/email_metrics.py` | Production metrics (`GET /v1/email/metrics`): throughput, P50/P95, fast/deep mix, cache hit rate, SMTP fallback rate, SMTP health, resolver precision. |

## Tables (migration `4678d41bd34a`)

* `domain_profiles` — one row per domain: provider, MX, accepts_mail, catch-all (+ confidence, method, date), SMTP reachability/greylisting, dominant pattern, sample counts, GitHub org, per-component freshness dates, evidence list, stats.
* `domain_email_samples` — real addresses observed per domain with source (`website`, `github`, `rdap`, `import`, `user`, `smtp_verified`, `search`), source URL, names when known, inferred pattern.
* `domain_email_patterns` — learned conventions: samples, SMTP successes/failures, share, confidence, `last_confirmed_at`, `last_failed_at`.
* `smtp_health` — health of **our** verification path (scope `global` and `provider:<name>`), sliding window, cooldown.
* `email_verification_requests` — deep-path work items (≤ 3 candidates each), `next_attempt_at` backoff, deferred delivery context.
* `email_resolver_stats` — empirical counters.
* `email_resolutions` — per-resolution log (path, status, resolver, candidates, SMTP probes, cache hits, duration).

## SMTP rules

* Never confuse "invalid mailbox" with "our IP is blocked": `X.7.x`, Spamhaus/RBL, rDNS, "client host" → infrastructure, never `INVALID`.
* `552` / `X.2.2` (mailbox full) means the mailbox **exists**.
* 4xx / 421 / greylisting / rate limits / timeouts → `temporary` → retry after 5 min, 30 min, 2 h (never under the 300 s greylisting default), then a verdict without `INVALID`.
* Catch-all: several random, plausible-looking local parts; `catch_all = true` only if all are accepted.
* Health: `BLOCKED` when most recent sessions fail at the infrastructure level across several domains (one dead MX is not enough) → probing stops with an exponential cooldown; verdicts fall back to evidence (`LIKELY_SAFE` / `RISKY` / `UNKNOWN`), never `INVALID`.
* Production note: Railway's Hobby plan blocks outbound port 25. The health monitor detects it; the engine then runs fast-path only, or the Go verifier service can be deployed where port 25 is open.
* Identity: the HELO domain must be a fully qualified domain we own with matching reverse DNS, and MAIL FROM on that domain (SPF). `.local` / `.example` identities disable probing outside tests.

## Deep-path policy (`engine.deep_plan`)

| Fast-path verdict | Deep path | Campaign delivery |
|---|---|---|
| `SAFE`, `INVALID`, catch-all domain, no MX, SMTP unavailable / `BLOCKED` | none | immediate (or rejected) |
| `LIKELY_SAFE` from a learned pattern backed by < 3 real addresses / SMTP successes | **confirm** in the background: success → `SAFE` and the domain learns; definitive rejection → corrected; inconclusive → the `LIKELY_SAFE` verdict is kept | immediate |
| `LIKELY_SAFE` from a proven convention or a published address | none | immediate |
| `RISKY`, `UNKNOWN`, `TEMPORARY_UNKNOWN` | **decide**: SMTP settles it | waits for the verdict (reservation kept) |

* **Lean probes** (`deep_candidates`): a confirmation probes the chosen address only; a guess backed by a
  learned pattern (≥ 0.6) probes its best rendering only; otherwise ≤ 3 candidates.
* **Expansion round** (`should_expand` / `expand_candidates`): when every guess was rejected as an unknown user
  by a healthy server on a domain proven not catch-all, and the domain has no proven convention, the next
  ≤ 3 patterns are tried once in the next per-domain batch. A rejected *published* address is not expanded
  (the person most likely left).
* SMTP reply wording rules ignore the addresses the server echoes back (a domain such as
  `greylist-agency.fr` must not turn `550 5.1.1 user unknown` into "greylisting").

## Performance rule

Avoid "3,000 people × 10 permutations × SMTP". Instead: domain intelligence → pattern learning →
1–3 candidates → SMTP only when ambiguous, batched per domain. Separate bounded pools for website,
DNS, public sources (GitHub/RDAP) and SMTP; fast-path leads are delivered immediately while the deep
path continues in the background (campaigns keep the reservation and deliver on conclusion).

## Benchmark

`scout/benchmark/email_bench.py` runs deterministic labelled scenarios (`email_scenarios.py`: published emails,
strong / weak conventions, unknown domains, catch-all, greylisting, accept-all gateways, no MX; ~8 % of people
have no mailbox) against the simulated mail world (`smtp/world.py`, same batching and classification code as
production) and compares strategies. Each dataset runs in two waves (a later campaign on the same domains), so
learning and caching are measured. The engine strategies call the production decision functions; only
persistence is replaced by in-memory state.

| Strategy | Meaning |
|---|---|
| `legacy` | v1 waterfall: ≤ 6 candidates, one SMTP session per address, stop at the first SAFE, no retries |
| `fast_only` | domain intelligence + learned pattern + ≤ 3 candidates, no SMTP |
| `fast_deep` | production policy: fast path + one batched SMTP session per ambiguous domain |
| `fast_deep_blocked` | `fast_deep` with outbound port 25 blocked (Railway Hobby) |

Run (1,000 synthetic domains, 1,845 people, seed 7, 2 waves — `APP_ENV=test`, ~15 s):

| Metric | legacy | fast_only | fast_deep | fast_deep_blocked |
|---|---|---|---|---|
| Discovery recall (SAFE + LIKELY_SAFE, right address) | 0.752 | 0.555 | **0.799** | 0.555 |
| Precision (SAFE + LIKELY_SAFE) | 0.998 | 0.935 | 0.960 | 0.935 |
| SAFE precision | 0.998 | 1.0 | 1.0 | 1.0 |
| Right address, any status | 0.811 | 0.762 | 0.869 | 0.762 |
| INVALID false-positive rate | 0 | 0 | 0 | 0 |
| Catch-all accuracy (share of domains known) | 1.0 (0.87) | — (0) | 1.0 (0.52) | — (0) |
| Domain pattern accuracy | 1.0 | 1.0 | 1.0 | 1.0 |
| P50 / P95 time to first verdict (ms) | 535 / 2,760 | 40 / 42 | 41 / 42 | 40 / 42 |
| P95 time to final verdict incl. greylist backoff (ms) | 2,760 | 42 | 300,921 | 42 |
| SMTP fallback rate | 0.95 | 0 | 0.45 | 0.003 |
| SMTP sessions / RCPT commands | 2,591 / 3,653 | 0 / 0 | **817** / 4,027 | 10 / 0 |
| Cost per email (assumed unit costs) | $5.7e-5 | $0 | $2.7e-5 | $2e-7 |
| Emails resolved per minute (bounded pools) | 128 | 22,808 | 506 | 574 |

Reading it honestly:

* The deep path recovers greylisting domains (legacy: UNKNOWN) and unknown conventions (expansion round), with
  zero INVALID false positives, and never probes once port 25 is known to be blocked (10 failed sessions, then
  the health monitor closes the gate; no INVALID verdict caused by it).
* `LIKELY_SAFE` without SMTP costs precision: in this world 8 % of people have no mailbox, so a proven
  convention is right ~95 % of the time. Campaigns that need certainty use "SAFE only".
* `fast_deep` sends 3× fewer SMTP sessions than `legacy` (one per domain instead of one per address) but ~10 %
  more RCPT commands (several candidates per batch). A pilot-person strategy for unknown domains is the next
  optimisation to evaluate with this harness.
* These are synthetic scenarios: they compare strategies and guard behaviour. No precision figure for real
  domains is claimed before a benchmark on a real labelled dataset (Benchmark Harness, `/benchmark`).

## Compliance

B2B emails are personal data under GDPR: outreach requires a legitimate-interest assessment, an
Article 14 notice and an opt-out (the suppression list enforces it everywhere). RCPT probing is
kept minimal (one session per domain, catch-all cached) to avoid abusive patterns and IP blocklisting.
No paid enrichment API is a dependency; reference projects were studied for ideas only (two have no
licence, OpenEnrich is AGPL-3.0 — no code was copied).
