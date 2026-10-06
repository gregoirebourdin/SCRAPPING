# Research — Security & Compliance

## 1. Authentication

* **Better Auth** (open source, self-hosted, no paid service) runs inside Next.js with the
  Neon/Postgres `pg` Pool. Email + password (scrypt hashing by Better Auth), optional Google
  OAuth when `GOOGLE_CLIENT_ID/SECRET` are set. Sessions are httpOnly, `Secure`,
  `SameSite=Lax` cookies; session tokens are stored server-side (`auth_sessions`).
* `proxy.ts` gates pages on cookie presence (fast path); every route handler and the BFF
  proxy re-validate the session with `auth.api.getSession` (authoritative).
* **Service-to-service**: the BFF mints a 60 s HS256 JWT (`aud=scout-api`, `iss=scout-web`,
  `sub=user_id`, `jti`) with `INTERNAL_API_SECRET` (≥ 32 random bytes). FastAPI rejects
  missing/expired/wrong-audience tokens. The secret never reaches the browser.

## 2. Authorization & workspace isolation

* FastAPI resolves `WorkspaceContext` from `(jwt.sub, X-Workspace-Id)` against
  `workspace_members`; absent membership → 403. Roles: owner > admin > member
  (budget/members/webhooks/suppression deletion require admin).
* Repositories accept the context and always filter by `workspace_id`; entity ids received
  from clients or the AI are re-checked for workspace ownership before any write
  (`ensure_owned(...)`), including ids inside `RowRef` selections.
* AI tools run with the same context as the user — the model can never act outside the
  user's workspace and never receives SQL capabilities.

## 3. Secrets

Gemini keys, database URLs, internal secrets and proxy credentials exist only in server
environment variables (Vercel/Railway). Nothing secret is prefixed `NEXT_PUBLIC_`.
Structured logs pass through a redaction processor (keys matching
`secret|token|password|authorization|api_key|database_url`).

## 4. Input validation

Zod on the client and BFF boundaries; Pydantic (`extra="forbid"`) on every API and AI tool
input; size limits on uploads (CSV ≤ 20 MB, ≤ 100k rows) and request bodies.

## 5. Scraping safety

* **SSRF**: only `http`/`https`, ports 80/443/8080/8443. Hostnames are resolved and every
  resolved address is checked; loopback, private (RFC 1918), CGNAT, link-local
  (incl. `169.254.169.254` metadata), multicast, reserved, unspecified and IPv6 ULA/link-local
  ranges are blocked. The check happens inside the connection backend (DNS-rebinding safe)
  and again on every redirect hop (max 5). Raw IP URLs to private ranges are rejected before
  resolution. Response size caps (3 MB HTML) and timeouts (connect 5 s, read 15 s).
* Scraped HTML is never rendered as HTML in the app. Stored text is extracted text; the
  only HTML stored is the home `<head>` for fingerprinting, never sent to the browser
  unescaped.
* **Prompt injection**: see AI_TOOLS.md §3.

## 6. CSV safety

Exports escape formula payloads: any cell starting with `=`, `+`, `-`, `@`, tab or carriage
return is prefixed with `'` (OWASP CSV injection guidance). Imports are parsed as data only.

## 7. Abuse & responsible crawling

Polite per-domain concurrency (≤ 2) and spacing, global pools, exponential backoff, robots.txt
honoured, identifiable User-Agent with contact URL (`CRAWLER_USER_AGENT`), only publicly
accessible pages, no login walls, no CAPTCHA solving, no paid proxy networks, no authenticated
LinkedIn scraping (public profile URLs may be stored when found through public search).

## 8. Compliance (GDPR-aware B2B prospecting)

* Provenance (source URL, source type, collection date) is stored for every personal data
  field, enabling Art. 14 information duties and access requests.
* Suppression list (opt-out, do-not-contact, GDPR request) is checked before every insertion
  path; suppressed people are never re-added silently. A GDPR erasure endpoint deletes the
  person's personal data while keeping a hashed suppression key so they are not rediscovered.
* Data minimization: only professional data relevant to B2B outreach is collected; no
  sensitive categories; retention policy for crawl cache.

## 9. Auditability

`audit_logs` records who/what/when/entities/tool call/campaign for every meaningful
operation, with undo payloads where an undo is realistically safe (list moves/removals,
renames, filters, manual edits, column creation). Irreversible external effects (exports
already downloaded, SMTP probes) are never presented as undoable.

## 10. Dependency hygiene

Pinned versions (pnpm lockfile, uv lockfile, Go modules), licenses verified (MIT/Apache-2.0
only for bundled components), `pnpm audit` / `pip-audit` in CI.
