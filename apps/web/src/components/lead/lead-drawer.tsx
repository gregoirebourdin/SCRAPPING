"use client";

import { Badge, Button, cn, ConfidenceMeter, EmailStatusBadge, FreshnessBadge, IconButton, Popover, PopoverContent, PopoverTrigger, Skeleton, Tip } from "@scout/design-system";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AnimatePresence, motion } from "motion/react";
import { Building2, Check, CircleAlert, Copy, ExternalLink, Globe, Info, Mail, MapPin, Phone, RefreshCw, Users, X } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { copyText, refreshRows } from "@/components/table/actions";
import { Avatar } from "@/components/table/cells";
import { api } from "@/lib/api";
import { employees, hostname, relTime, shortDate } from "@/lib/format";
import { qk } from "@/lib/queries";
import { useUI } from "@/lib/store";

/* ---- payload types (GET /v1/people/{id}, /v1/companies/{id}) ------------------------------------ */

export interface Observation {
  id: string;
  field: string;
  value: unknown;
  source_type: string;
  source_key: string | null;
  source_url: string | null;
  source_title: string | null;
  source_label: string;
  evidence: string | null;
  confidence: number | null;
  user_confirmed: boolean;
  observed_at: string;
  freshness: string;
}

interface EmailInfo {
  id: string;
  address: string;
  status: string;
  kind: string;
  discovery_method: string;
  pattern: string | null;
  source_url: string | null;
  mx_valid: boolean | null;
  smtp_result: string;
  catch_all: boolean | null;
  disposable: boolean | null;
  role_address: boolean | null;
  free_provider: boolean | null;
  overall_confidence: number | null;
  is_primary: boolean;
  last_checked_at: string | null;
  freshness: string;
  checks: { verifier: string; status: string; smtp_result: string; catch_all: boolean | null; checked_at: string; error: string | null }[];
  explanation?: { name: string; weight: number; detail: string; source?: string | null; source_url?: string | null }[];
  resolution_path?: string | null;
  resolver?: string | null;
  name_affinity?: number | null;
}

interface EmailIntel {
  domain: string;
  provider: string | null;
  mx_hosts: string[];
  accepts_mail: boolean | null;
  catch_all: boolean | null;
  catch_all_checked_at: string | null;
  smtp_reachable: boolean | null;
  greylisting_seen: boolean;
  named_samples: number;
  observed_emails: number;
  updated_at: string | null;
  patterns: { pattern: string; confidence: number; share: number; samples: number; successes: number; failures: number; last_confirmed_at: string | null }[];
}

interface Enrichment {
  column_id: string;
  column: string;
  data_type: string;
  kind: string;
  entity_type: string;
  value: unknown;
  display: string | null;
  status: string;
  confidence: number | null;
  evidence: string | null;
  source_url: string | null;
  resolver: string | null;
  user_override: boolean;
  error: string | null;
  observed_at: string;
  freshness: string;
}

interface CompanyDetail {
  id: string;
  name: string;
  domain: string | null;
  website_url: string | null;
  description: string | null;
  country: string | null;
  region: string | null;
  city: string | null;
  postal_code: string | null;
  address: string | null;
  industry: string | null;
  sub_industry: string | null;
  category_raw: string | null;
  employee_min: number | null;
  employee_max: number | null;
  phone: string | null;
  linkedin_url: string | null;
  registry_source: string | null;
  registry_id: string | null;
  founded_year: number | null;
  status: string;
  website_status: string;
  company_confidence: number | null;
  has_conflicts: boolean;
  needs_review: boolean;
  first_seen_at: string;
  last_crawled_at: string | null;
  times_discovered: number;
  times_exported: number;
  freshness: string;
  observations: Record<string, Observation[]>;
  people: { id: string; full_name: string; job_title: string | null; role_family: string | null; identity_confidence: number | null }[];
  technologies: {
    name: string;
    category: string | null;
    version: string | null;
    confidence: number | null;
    detector: string | null;
    source_url: string | null;
    observed_at: string;
  }[];
  signals: { id: string; type: string; value: unknown; source_url: string | null; evidence: string | null; confidence: number | null; observed_at: string }[];
  pages: { id: string; url: string; page_type: string; title: string | null; fetched_at: string | null }[];
  company_emails: { address: string; status: string; kind: string; source_url: string | null }[];
  email_intel?: EmailIntel | null;
  lists?: { id: string; name: string; added_at: string }[];
  campaigns?: { id: string; name: string; status: string; first_discovered_at: string }[];
  enrichments?: Enrichment[];
}

interface PersonDetail {
  id: string;
  full_name: string;
  job_title: string | null;
  normalized_title: string | null;
  department: string | null;
  seniority: string | null;
  role_family: string | null;
  decision_power: number | null;
  public_profile_url: string | null;
  location: string | null;
  phone: string | null;
  identity_confidence: number | null;
  needs_review: boolean;
  has_conflicts: boolean;
  first_seen_at: string;
  last_verified_at: string | null;
  times_discovered: number;
  times_exported: number;
  last_exported_at: string | null;
  contacted_at: string | null;
  suppressed_at: string | null;
  freshness: string;
  observations: Record<string, Observation[]>;
  emails: EmailInfo[];
  email_verifying?: boolean;
  score: {
    icp_score: number;
    qualified: boolean;
    company_fit: number | null;
    person_fit: number | null;
    intent: number | null;
    contactability: number | null;
    evidence: number | null;
    overall_confidence: number | null;
    gates: { gate: string; passed: boolean; reason: string | null }[];
    explanation: string[];
    computed_at: string;
  } | null;
  company: CompanyDetail | null;
  lists?: { id: string; name: string; added_at: string }[];
  campaigns?: { id: string; name: string; status: string; first_discovered_at: string }[];
  enrichments?: Enrichment[];
}

interface HistoryEvent {
  at: string;
  type: string;
  label: string;
  detail?: string | null;
  source_url?: string | null;
  campaign_id?: string | null;
  list_id?: string | null;
}

/* ---- drawer shell ------------------------------------------------------------------------------ */

export function LeadDrawer() {
  const drawer = useUI((s) => s.drawer);
  const close = useUI((s) => s.closeDrawer);
  return (
    <AnimatePresence>
      {drawer && (
        <motion.aside
          key="drawer"
          initial={{ x: 24, opacity: 0 }}
          animate={{ x: 0, opacity: 1 }}
          exit={{ x: 24, opacity: 0 }}
          transition={{ duration: 0.18, ease: [0.2, 0, 0, 1] }}
          role="dialog"
          aria-label="Lead details"
          className="fixed inset-y-0 right-0 z-40 flex w-full flex-col border-l border-line bg-surface-1 shadow-dialog sm:w-[480px]"
        >
          {drawer.entityType === "person" ? <PersonView id={drawer.id} onClose={close} /> : <CompanyView id={drawer.id} onClose={close} />}
        </motion.aside>
      )}
    </AnimatePresence>
  );
}

type Tab = "overview" | "sources" | "history";

function Tabs({ tab, onTab }: { tab: Tab; onTab: (t: Tab) => void }) {
  return (
    <div className="flex h-9 shrink-0 items-center gap-0.5 border-b border-line px-3">
      {(["overview", "sources", "history"] as Tab[]).map((t) => (
        <button
          key={t}
          type="button"
          onClick={() => onTab(t)}
          className={cn(
            "relative h-7 rounded-sm px-2.5 text-meta font-medium capitalize",
            tab === t ? "text-fg after:absolute after:inset-x-2 after:-bottom-[5px] after:h-[2px] after:rounded-full after:bg-accent" : "text-fg-3 hover:text-fg-2",
          )}
        >
          {t}
        </button>
      ))}
    </div>
  );
}

function DrawerSkeleton({ onClose }: { onClose: () => void }) {
  return (
    <>
      <div className="flex items-start gap-3 border-b border-line p-4">
        <Skeleton className="size-10 rounded-full" />
        <div className="flex-1 space-y-2">
          <Skeleton className="h-4 w-40" />
          <Skeleton className="h-3 w-56" />
        </div>
        <IconButton label="Close" onClick={onClose}>
          <X className="size-4" />
        </IconButton>
      </div>
      <div className="space-y-3 p-4">
        {Array.from({ length: 8 }).map((_, i) => (
          <Skeleton key={i} className="h-3" />
        ))}
      </div>
    </>
  );
}

function ErrorState({ message, onClose }: { message: string; onClose: () => void }) {
  return (
    <div className="flex flex-1 flex-col">
      <div className="flex justify-end p-2">
        <IconButton label="Close" onClick={onClose}>
          <X className="size-4" />
        </IconButton>
      </div>
      <div className="grid flex-1 place-items-center p-6 text-center">
        <div>
          <CircleAlert className="mx-auto size-5 text-danger" />
          <p className="mt-2 text-body text-fg">Could not load this lead</p>
          <p className="mt-1 text-meta text-fg-3">{message}</p>
        </div>
      </div>
    </div>
  );
}

/* ---- person ------------------------------------------------------------------------------------ */

function PersonView({ id, onClose }: { id: string; onClose: () => void }) {
  const [tab, setTab] = useState<Tab>("overview");
  const qc = useQueryClient();
  const q = useQuery({ queryKey: qk.person(id), queryFn: () => api<PersonDetail>(`people/${id}`) });
  if (q.isLoading) return <DrawerSkeleton onClose={onClose} />;
  if (q.isError || !q.data) return <ErrorState message={(q.error as Error)?.message ?? "Not found"} onClose={onClose} />;
  const p = q.data;
  const c = p.company;
  const email = p.emails.find((e) => e.is_primary) ?? p.emails[0];
  const ref = { entity_type: "person" as const, ids: [p.id] };

  return (
    <>
      <div className="flex items-start gap-3 border-b border-line px-4 pb-3 pt-4">
        <Avatar name={p.full_name} size={40} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h2 className="title-gradient truncate text-title tracking-[-0.01em]">{p.full_name}</h2>
            {p.needs_review && <Badge tone="warning">Review</Badge>}
            {p.suppressed_at && <Badge tone="danger">Suppressed</Badge>}
          </div>
          <p className="truncate text-body text-fg-2">
            {p.job_title ?? "No title"}
            {c ? (
              <>
                {" · "}
                <button type="button" className="text-fg hover:underline" onClick={() => useUI.getState().openDrawer("company", c.id)}>
                  {c.name}
                </button>
              </>
            ) : null}
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-1">
            {email && (
              <Button size="xs" variant="secondary" onClick={() => copyText(email.address)}>
                <Mail /> {email.address}
              </Button>
            )}
            {p.public_profile_url && (
              <a
                href={p.public_profile_url}
                target="_blank"
                rel="noreferrer noopener"
                className="inline-flex h-6 items-center gap-1 rounded-sm px-2 text-meta text-fg-2 hover:bg-surface-2 hover:text-fg"
              >
                <InMark /> Profile
              </a>
            )}
            {c?.website_url && (
              <a
                href={c.website_url}
                target="_blank"
                rel="noreferrer noopener"
                className="inline-flex h-6 items-center gap-1 rounded-sm px-2 text-meta text-fg-2 hover:bg-surface-2 hover:text-fg"
              >
                <Globe className="size-3.5" /> {hostname(c.website_url)}
              </a>
            )}
          </div>
        </div>
        <div className="flex items-center gap-0.5">
          <Tip content="Refresh person & email">
            <IconButton
              label="Refresh"
              onClick={async () => {
                await refreshRows(qc, ref, "person");
                void qc.invalidateQueries({ queryKey: qk.person(id) });
              }}
            >
              <RefreshCw className="size-3.5" />
            </IconButton>
          </Tip>
          <IconButton label="Close" onClick={onClose}>
            <X className="size-4" />
          </IconButton>
        </div>
      </div>
      <Tabs tab={tab} onTab={setTab} />
      <div className="min-h-0 flex-1 overflow-y-auto">
        {tab === "overview" && (
          <div className="divide-y divide-line">
            <Section title="Person">
              <Field label="Name" value={p.full_name} obs={p.observations.full_name} />
              <Field label="Title" value={p.job_title} obs={p.observations.job_title} />
              <Field
                label="Role"
                value={
                  [p.seniority, p.role_family, p.department]
                    .filter(Boolean)
                    .map((x) => String(x).replace(/_/g, " "))
                    .join(" · ") || null
                }
              />
              <Field
                label="Profile"
                value={p.public_profile_url ? hostname(p.public_profile_url) + new URL(p.public_profile_url).pathname : null}
                href={p.public_profile_url}
                obs={p.observations.public_profile_url}
              />
              <Field label="Location" value={p.location} obs={p.observations.location} />
              <Field label="Identity" value={<ConfidenceMeter value={p.identity_confidence} />} />
            </Section>
            <Section
              title="Contact"
              aside={
                p.email_verifying ? (
                  <Badge tone="info" dot>
                    Verifying email…
                  </Badge>
                ) : undefined
              }
            >
              {p.emails.length === 0 && <Empty>{p.email_verifying ? "Verification in progress" : "No email found yet"}</Empty>}
              {p.emails.map((e) => (
                <EmailRow key={e.id} e={e} />
              ))}
              {p.phone && <Field label="Phone" value={p.phone} obs={p.observations.phone} />}
            </Section>
            {p.score && (
              <Section title="Qualification" aside={<Badge tone={p.score.qualified ? "success" : "warning"}>{p.score.qualified ? "Qualified" : "Not qualified"}</Badge>}>
                <div className="mb-2 grid grid-cols-5 gap-2">
                  <ScoreTile label="ICP" value={p.score.icp_score} big />
                  <ScoreTile label="Company" value={pct100(p.score.company_fit)} />
                  <ScoreTile label="Person" value={pct100(p.score.person_fit)} />
                  <ScoreTile label="Contact" value={pct100(p.score.contactability)} />
                  <ScoreTile label="Evidence" value={pct100(p.score.evidence)} />
                </div>
                {p.score.explanation?.length > 0 && (
                  <ul className="space-y-1">
                    {p.score.explanation.map((b) => (
                      <li key={b} className="flex gap-1.5 text-meta text-fg-2">
                        <Check className="mt-0.5 size-3 shrink-0 text-success" />
                        {b}
                      </li>
                    ))}
                  </ul>
                )}
                {p.score.gates?.some((g) => !g.passed) && (
                  <ul className="mt-2 space-y-1">
                    {p.score.gates
                      .filter((g) => !g.passed)
                      .map((g) => (
                        <li key={g.gate} className="flex gap-1.5 text-meta text-warning">
                          <CircleAlert className="mt-0.5 size-3 shrink-0" />
                          {g.reason ?? g.gate.replace(/_/g, " ")}
                        </li>
                      ))}
                  </ul>
                )}
              </Section>
            )}
            {c && <CompanySections c={c} compact />}
            <EnrichmentSection items={p.enrichments ?? []} />
            <MembershipSection lists={p.lists} campaigns={p.campaigns} />
            <Section title="Activity">
              <Field label="First seen" value={shortDate(p.first_seen_at)} />
              <Field label="Discovered" value={`${p.times_discovered}×`} />
              <Field label="Exported" value={p.times_exported ? `${p.times_exported}× · last ${relTime(p.last_exported_at)}` : "Never"} />
              <Field label="Contacted" value={p.contacted_at ? shortDate(p.contacted_at) : "No"} />
            </Section>
          </div>
        )}
        {tab === "sources" && <SourcesTab groups={[{ title: "Person", obs: p.observations }, ...(c ? [{ title: c.name, obs: c.observations }] : [])]} />}
        {tab === "history" && <HistoryTab entity="people" id={p.id} />}
      </div>
    </>
  );
}

/* ---- company ----------------------------------------------------------------------------------- */

function CompanyView({ id, onClose }: { id: string; onClose: () => void }) {
  const [tab, setTab] = useState<Tab>("overview");
  const qc = useQueryClient();
  const q = useQuery({ queryKey: qk.company(id), queryFn: () => api<CompanyDetail>(`companies/${id}`) });
  if (q.isLoading) return <DrawerSkeleton onClose={onClose} />;
  if (q.isError || !q.data) return <ErrorState message={(q.error as Error)?.message ?? "Not found"} onClose={onClose} />;
  const c = q.data;
  return (
    <>
      <div className="flex items-start gap-3 border-b border-line px-4 pb-3 pt-4">
        <span className="grid size-10 shrink-0 place-items-center rounded-md bg-surface-3 text-fg-2">
          <Building2 className="size-5" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h2 className="title-gradient truncate text-title tracking-[-0.01em]">{c.name}</h2>
            {c.needs_review && <Badge tone="warning">Review</Badge>}
          </div>
          <p className="truncate text-body text-fg-2">{[c.industry, [c.city, c.country].filter(Boolean).join(", ")].filter(Boolean).join(" · ") || "—"}</p>
          <div className="mt-2 flex flex-wrap items-center gap-1">
            {c.website_url && (
              <a
                href={c.website_url}
                target="_blank"
                rel="noreferrer noopener"
                className="inline-flex h-6 items-center gap-1 rounded-sm px-2 text-meta text-fg-2 hover:bg-surface-2 hover:text-fg"
              >
                <Globe className="size-3.5" /> {hostname(c.website_url)}
              </a>
            )}
            {c.linkedin_url && (
              <a
                href={c.linkedin_url}
                target="_blank"
                rel="noreferrer noopener"
                className="inline-flex h-6 items-center gap-1 rounded-sm px-2 text-meta text-fg-2 hover:bg-surface-2 hover:text-fg"
              >
                <InMark /> Page
              </a>
            )}
          </div>
        </div>
        <div className="flex items-center gap-0.5">
          <Tip content="Re-crawl website & refresh">
            <IconButton
              label="Refresh"
              onClick={async () => {
                await refreshRows(qc, { entity_type: "company", ids: [c.id] }, "company");
                void qc.invalidateQueries({ queryKey: qk.company(id) });
              }}
            >
              <RefreshCw className="size-3.5" />
            </IconButton>
          </Tip>
          <IconButton label="Close" onClick={onClose}>
            <X className="size-4" />
          </IconButton>
        </div>
      </div>
      <Tabs tab={tab} onTab={setTab} />
      <div className="min-h-0 flex-1 overflow-y-auto">
        {tab === "overview" && (
          <div className="divide-y divide-line">
            <CompanySections c={c} />
            {c.email_intel && <DomainIntelSection intel={c.email_intel} />}
            <EnrichmentSection items={c.enrichments ?? []} />
            <MembershipSection lists={c.lists} campaigns={c.campaigns} />
            {c.pages.length > 0 && (
              <Section title={`Crawled pages (${c.pages.length})`}>
                {c.pages.map((pg) => (
                  <a key={pg.id} href={pg.url} target="_blank" rel="noreferrer noopener" className="flex items-center gap-2 rounded-sm px-1 py-0.5 text-meta hover:bg-surface-2">
                    <Badge tone="neutral" className="w-20 justify-center">
                      {pg.page_type.replace(/_/g, " ")}
                    </Badge>
                    <span className="min-w-0 flex-1 truncate text-fg-2">{pg.title || new URL(pg.url).pathname}</span>
                    <span className="text-fg-3">{relTime(pg.fetched_at)}</span>
                  </a>
                ))}
              </Section>
            )}
          </div>
        )}
        {tab === "sources" && <SourcesTab groups={[{ title: c.name, obs: c.observations }]} />}
        {tab === "history" && <HistoryTab entity="companies" id={c.id} />}
      </div>
    </>
  );
}

function CompanySections({ c, compact }: { c: CompanyDetail; compact?: boolean }) {
  const openDrawer = useUI((s) => s.openDrawer);
  return (
    <>
      <Section
        title="Company"
        aside={
          compact ? (
            <button type="button" onClick={() => openDrawer("company", c.id)} className="text-meta text-accent-strong hover:underline">
              Open company
            </button>
          ) : (
            <FreshnessBadge freshness={c.freshness} />
          )
        }
      >
        <Field label="Name" value={c.name} obs={c.observations.name} />
        <Field
          label="Website"
          value={c.website_url ? hostname(c.website_url) : null}
          href={c.website_url}
          obs={c.observations.website_url}
          extra={c.website_status !== "ok" && c.website_status !== "unknown" ? <Badge tone="warning">{c.website_status.replace(/_/g, " ")}</Badge> : null}
        />
        {!compact && <Field label="Description" value={c.description} obs={c.observations.description} multiline />}
        <Field label="Industry" value={c.industry ?? c.category_raw} obs={c.observations.industry} />
        <Field
          label="Size"
          value={c.employee_min != null || c.employee_max != null ? `${employees(c.employee_min, c.employee_max)} employees` : null}
          obs={c.observations.employee_count}
        />
        <Field
          label="Location"
          value={[c.address, c.postal_code, c.city, c.country].filter(Boolean).join(", ") || null}
          obs={c.observations.city ?? c.observations.address}
          icon={<MapPin className="size-3" />}
        />
        {!compact && <Field label="Phone" value={c.phone} obs={c.observations.phone} icon={<Phone className="size-3" />} />}
        {(c.registry_id || !compact) && (
          <Field label="Registry" value={c.registry_id ? `${c.registry_id}${c.registry_source ? ` · ${c.registry_source}` : ""}` : null} obs={c.observations.registry_id} />
        )}
        {!compact && c.founded_year && <Field label="Founded" value={String(c.founded_year)} obs={c.observations.founded_year} />}
        <Field label="Confidence" value={<ConfidenceMeter value={c.company_confidence} />} />
      </Section>
      {!compact && (c.people.length > 0 || c.company_emails.length > 0) && (
        <Section title={`People (${c.people.length})`} aside={<Users className="size-3.5 text-fg-3" />}>
          {c.people.map((p) => (
            <button
              key={p.id}
              type="button"
              onClick={() => openDrawer("person", p.id)}
              className="flex w-full items-center gap-2 rounded-sm px-1 py-1 text-left hover:bg-surface-2"
            >
              <Avatar name={p.full_name} size={20} />
              <span className="min-w-0 flex-1 truncate text-body text-fg">{p.full_name}</span>
              <span className="max-w-[45%] truncate text-meta text-fg-3">{p.job_title}</span>
            </button>
          ))}
          {c.company_emails.map((e) => (
            <div key={e.address} className="flex items-center gap-2 px-1 py-1 text-meta">
              <Mail className="size-3 text-fg-3" />
              <span className="font-mono text-[12px] text-fg-2">{e.address}</span>
              <Badge tone="muted">{e.kind}</Badge>
              <EmailStatusBadge status={e.status} />
            </div>
          ))}
        </Section>
      )}
      {c.technologies.length > 0 && (
        <Section title="Technology">
          <div className="flex flex-wrap gap-1">
            {c.technologies.map((t) => (
              <Tip
                key={t.name}
                content={`${t.category ?? "Technology"}${t.version ? ` ${t.version}` : ""} · ${t.detector ?? "fingerprint"}${t.confidence != null ? ` · ${Math.round(t.confidence * 100)}%` : ""}`}
              >
                <span className="inline-flex h-6 items-center rounded-sm bg-surface-2 px-2 text-meta text-fg-2 shadow-[inset_0_0_0_1px_var(--border-subtle)]">{t.name}</span>
              </Tip>
            ))}
          </div>
        </Section>
      )}
      {c.signals.length > 0 && (
        <Section title="Signals">
          {c.signals.map((s) => (
            <div key={s.id} className="flex items-start gap-2 py-0.5 text-meta">
              <Badge tone="info">{s.type.replace(/_/g, " ").toLowerCase()}</Badge>
              <span className="min-w-0 flex-1 text-fg-2">{s.evidence ?? (typeof s.value === "string" ? s.value : JSON.stringify(s.value))}</span>
              {s.source_url && (
                <a href={s.source_url} target="_blank" rel="noreferrer noopener" className="text-fg-3 hover:text-fg" aria-label="Open source">
                  <ExternalLink className="size-3" />
                </a>
              )}
            </div>
          ))}
        </Section>
      )}
    </>
  );
}

/* ---- building blocks ----------------------------------------------------------------------------- */

function InMark() {
  return <span className="rounded-[3px] bg-fg/12 px-[3px] text-[9px] font-bold leading-[13px] text-fg-2">in</span>;
}

function Section({ title, aside, children }: { title: string; aside?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="px-4 py-3">
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-micro font-medium uppercase tracking-wide text-fg-3">{title}</h3>
        {aside}
      </div>
      <div className="space-y-1">{children}</div>
    </section>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <p className="text-meta text-fg-3">{children}</p>;
}

function pct100(v: number | null | undefined): number | null {
  return v == null ? null : Math.round(v * 100);
}

function ScoreTile({ label, value, big }: { label: string; value: number | null; big?: boolean }) {
  return (
    <div className="rounded-sm bg-surface-2 px-2 py-1.5 shadow-[inset_0_0_0_1px_var(--border-subtle)]">
      <div className="text-micro text-fg-3">{label}</div>
      <div className={cn("tabular font-medium", big ? "text-title text-fg" : "text-body text-fg-2")}>{value ?? "—"}</div>
    </div>
  );
}

/** A field row with the trust layer: best value, confidence, and a source inspector (spec §84). */
function Field({
  label,
  value,
  obs,
  href,
  multiline,
  icon,
  extra,
}: {
  label: string;
  value: React.ReactNode;
  obs?: Observation[];
  href?: string | null;
  multiline?: boolean;
  icon?: React.ReactNode;
  extra?: React.ReactNode;
}) {
  const best = obs?.[0];
  const empty = value === null || value === undefined || value === "";
  return (
    <div className="group/f grid grid-cols-[96px_1fr] items-start gap-2 py-0.5">
      <span className="pt-px text-meta text-fg-3">{label}</span>
      <div className="flex min-w-0 items-start gap-1.5">
        <div className={cn("min-w-0 flex-1 text-body", empty ? "text-fg-3" : "text-fg", !multiline && "truncate")}>
          {empty ? (
            "—"
          ) : href ? (
            <a href={href} target="_blank" rel="noreferrer noopener" className="inline-flex max-w-full items-center gap-1 hover:text-accent-strong">
              {icon}
              <span className="truncate">{value}</span>
            </a>
          ) : (
            <span className={cn("inline-flex max-w-full items-center gap-1", multiline && "whitespace-pre-line")}>
              {icon}
              {multiline ? value : <span className="truncate">{value}</span>}
            </span>
          )}
        </div>
        {extra}
        {!empty && typeof value === "string" && (
          <button type="button" onClick={() => copyText(value)} className="invisible mt-0.5 text-fg-3 hover:text-fg group-hover/f:visible" aria-label={`Copy ${label}`}>
            <Copy className="size-3" />
          </button>
        )}
        {best && <SourceInspector label={label} obs={obs!} />}
      </div>
    </div>
  );
}

function SourceInspector({ label, obs }: { label: string; obs: Observation[] }) {
  const best = obs[0]!;
  const conflicting = new Set(obs.map((o) => JSON.stringify(o.value))).size > 1;
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          className={cn("mt-px inline-flex shrink-0 items-center gap-1 rounded-xs px-1 text-micro text-fg-3 hover:bg-surface-2 hover:text-fg", conflicting && "text-warning")}
          aria-label={`View sources for ${label}`}
        >
          {best.user_confirmed ? <Check className="size-3 text-accent" /> : <Info className="size-3" />}
          {best.confidence != null ? `${Math.round(best.confidence * 100)}%` : ""}
          {obs.length > 1 ? ` · ${obs.length}` : ""}
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80 p-0">
        <div className="border-b border-line px-3 py-2 text-meta font-medium text-fg">
          {label} · {obs.length} source{obs.length === 1 ? "" : "s"}
          {conflicting && <span className="ml-1 text-warning">· conflicting values</span>}
        </div>
        <div className="max-h-72 divide-y divide-line overflow-y-auto">
          {obs.map((o) => (
            <div key={o.id} className="space-y-1 px-3 py-2">
              <div className="flex items-center justify-between gap-2">
                <span className="truncate text-meta font-medium text-fg">{formatValue(o.value)}</span>
                {o.confidence != null && <span className="tabular shrink-0 text-meta text-fg-2">{Math.round(o.confidence * 100)}%</span>}
              </div>
              <div className="flex items-center gap-1.5 text-micro text-fg-3">
                <span className="text-fg-2">{o.source_label}</span>
                <span>· {shortDate(o.observed_at)}</span>
                <FreshnessBadge freshness={o.freshness} />
              </div>
              {o.evidence && <p className="line-clamp-3 rounded-xs bg-surface-2 px-1.5 py-1 text-micro italic text-fg-2">“{o.evidence}”</p>}
              {o.source_url && (
                <a href={o.source_url} target="_blank" rel="noreferrer noopener" className="inline-flex items-center gap-1 text-micro text-accent-strong hover:underline">
                  <ExternalLink className="size-3" /> {o.source_title || hostname(o.source_url)}
                </a>
              )}
            </div>
          ))}
        </div>
      </PopoverContent>
    </Popover>
  );
}

function formatValue(v: unknown): string {
  if (v == null) return "—";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  if (typeof v === "object" && v && "min" in v) {
    const r = v as { min?: number | null; max?: number | null };
    return employees(r.min, r.max);
  }
  return JSON.stringify(v);
}

function EmailRow({ e }: { e: EmailInfo }) {
  const [open, setOpen] = useState(false);
  const method = e.discovery_method.replace(/_/g, " ");
  return (
    <div className="rounded-sm bg-surface-2/60 px-2 py-1.5 shadow-[inset_0_0_0_1px_var(--border-subtle)]">
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate font-mono text-[12px] text-fg">{e.address}</span>
        {e.is_primary && <Badge tone="muted">primary</Badge>}
        <EmailStatusBadge status={e.status} />
        <button type="button" onClick={() => copyText(e.address)} className="text-fg-3 hover:text-fg" aria-label="Copy email">
          <Copy className="size-3" />
        </button>
      </div>
      <div className="mt-1 flex flex-wrap gap-x-2 gap-y-0.5 text-micro text-fg-3">
        <span>
          {method}
          {e.pattern ? ` · pattern ${e.pattern}` : ""}
        </span>
        {e.mx_valid != null && <span>MX {e.mx_valid ? "✓" : "✗"}</span>}
        {e.smtp_result && e.smtp_result !== "not_checked" && <span>SMTP {e.smtp_result.replace(/_/g, " ")}</span>}
        {e.catch_all && <span className="text-warning">catch-all domain</span>}
        {e.role_address && <span>role address</span>}
        {e.free_provider && <span>free provider</span>}
        {e.last_checked_at && <span>checked {relTime(e.last_checked_at)}</span>}
        {e.source_url && (
          <a href={e.source_url} target="_blank" rel="noreferrer noopener" className="text-accent-strong hover:underline">
            published here
          </a>
        )}
        {(e.explanation?.length ?? 0) > 0 && (
          <button type="button" onClick={() => setOpen((v) => !v)} className="text-accent-strong hover:underline">
            {open ? "Hide why" : "Why?"}
          </button>
        )}
      </div>
      {open && e.explanation && (
        <ul className="mt-1.5 space-y-0.5 border-t border-line pt-1.5">
          {e.explanation.map((sig, i) => (
            <li key={`${sig.name}-${i}`} className="flex gap-1.5 text-micro">
              <span className={cn("tabular w-9 shrink-0 text-right", sig.weight > 0 ? "text-success" : sig.weight < 0 ? "text-danger" : "text-fg-3")}>
                {sig.name === "base" ? "base" : sig.weight > 0 ? `+${sig.weight.toFixed(1)}` : sig.weight.toFixed(1)}
              </span>
              <span className="min-w-0 flex-1 text-fg-2">{sig.detail}</span>
            </li>
          ))}
          {e.resolution_path && <li className="text-micro text-fg-3">Resolved by the {e.resolution_path === "deep" ? "deep path (SMTP)" : "fast path (no SMTP)"}</li>}
        </ul>
      )}
    </div>
  );
}

const PROVIDER_LABELS: Record<string, string> = {
  google_workspace: "Google Workspace",
  microsoft_365: "Microsoft 365",
  secure_gateway: "Secure email gateway",
  self_hosted: "Self-hosted",
  none: "No mail server",
  unknown: "Unknown",
};

function DomainIntelSection({ intel }: { intel: EmailIntel }) {
  const providerLabel = intel.provider ? (PROVIDER_LABELS[intel.provider] ?? intel.provider.replace(/_/g, " ")) : "—";
  return (
    <Section title="Email intelligence" aside={<span className="text-micro text-fg-3">{intel.domain}</span>}>
      <Field label="Provider" value={providerLabel} />
      <Field label="Accepts mail" value={intel.accepts_mail === false ? "No (no MX)" : intel.mx_hosts.length ? intel.mx_hosts.join(", ") : intel.accepts_mail ? "Yes" : null} />
      <Field
        label="Catch-all"
        value={intel.catch_all == null ? "Not tested" : intel.catch_all ? "Yes — guessed addresses cannot be confirmed" : "No"}
        extra={intel.catch_all_checked_at ? <span className="shrink-0 text-micro text-fg-3">{relTime(intel.catch_all_checked_at)}</span> : null}
      />
      {intel.greylisting_seen && <Field label="SMTP" value="Greylisting observed (retries scheduled)" />}
      {intel.patterns.length > 0 ? (
        <div className="space-y-1 pt-1">
          {intel.patterns.map((p) => (
            <div key={p.pattern} className="grid grid-cols-[96px_1fr_auto] items-center gap-2 text-meta">
              <span className="font-mono text-[12px] text-fg">{p.pattern}</span>
              <span className="h-1 overflow-hidden rounded-full bg-surface-3">
                <span className="block h-full rounded-full bg-accent" style={{ width: `${Math.round((p.share || p.confidence) * 100)}%` }} />
              </span>
              <span className="tabular text-fg-2" title={`${p.samples} real emails · ${p.successes} SMTP-confirmed · ${p.failures} rejected`}>
                {Math.round(p.confidence * 100)}% · {p.samples + p.successes} ev.
              </span>
            </div>
          ))}
        </div>
      ) : (
        <Empty>No email convention learned yet for this domain</Empty>
      )}
      <p className="pt-1 text-micro text-fg-3">
        {intel.named_samples} named sample{intel.named_samples === 1 ? "" : "s"} · {intel.observed_emails} observed address{intel.observed_emails === 1 ? "" : "es"}
        {intel.updated_at ? ` · updated ${relTime(intel.updated_at)}` : ""}
      </p>
    </Section>
  );
}

function EnrichmentSection({ items }: { items: Enrichment[] }) {
  if (!items.length) return null;
  return (
    <Section title="Enrichments">
      {items.map((e) => (
        <div key={`${e.column_id}:${e.entity_type}`} className="grid grid-cols-[96px_1fr] items-start gap-2 py-0.5">
          <span className="truncate pt-px text-meta text-fg-3" title={e.column}>
            {e.column}
          </span>
          <div className="flex min-w-0 items-start gap-1.5">
            <span className={cn("min-w-0 flex-1 text-body", e.status === "success" ? "text-fg" : "text-fg-3", e.status === "unknown" && "italic")}>
              {e.status === "success"
                ? e.data_type === "boolean"
                  ? e.value === true
                    ? "Yes"
                    : "No"
                  : (e.display ?? formatValue(e.value))
                : e.status === "failed"
                  ? (e.error ?? "Failed")
                  : e.status.replace(/_/g, " ")}
            </span>
            {(e.evidence || e.source_url) && (
              <Popover>
                <PopoverTrigger asChild>
                  <button
                    type="button"
                    className="mt-px inline-flex items-center gap-1 rounded-xs px-1 text-micro text-fg-3 hover:bg-surface-2 hover:text-fg"
                    aria-label={`Evidence for ${e.column}`}
                  >
                    {e.user_override ? <Check className="size-3 text-accent" /> : <Info className="size-3" />}
                    {e.confidence != null ? `${Math.round(e.confidence * 100)}%` : ""}
                  </button>
                </PopoverTrigger>
                <PopoverContent align="end" className="w-80 space-y-1.5">
                  <div className="text-meta font-medium text-fg">{e.column}</div>
                  <div className="text-micro text-fg-3">
                    {(e.resolver ?? "").replace(/_/g, " ")} · {shortDate(e.observed_at)} {e.user_override && "· edited by you"}
                  </div>
                  {e.evidence && <p className="rounded-xs bg-surface-2 px-1.5 py-1 text-micro italic text-fg-2">“{e.evidence}”</p>}
                  {e.source_url && (
                    <a href={e.source_url} target="_blank" rel="noreferrer noopener" className="inline-flex items-center gap-1 text-micro text-accent-strong hover:underline">
                      <ExternalLink className="size-3" /> {hostname(e.source_url)}
                    </a>
                  )}
                </PopoverContent>
              </Popover>
            )}
          </div>
        </div>
      ))}
    </Section>
  );
}

function MembershipSection({
  lists,
  campaigns,
}: {
  lists?: { id: string; name: string }[];
  campaigns?: { id: string; name: string; status: string; first_discovered_at: string }[];
}) {
  if (!lists?.length && !campaigns?.length) return null;
  return (
    <Section title="Lists & campaigns">
      {lists?.length ? (
        <div className="flex flex-wrap gap-1">
          {lists.map((l) => (
            <Link
              key={l.id}
              href={`/lists/${l.id}`}
              className="inline-flex h-6 items-center rounded-sm bg-surface-2 px-2 text-meta text-fg-2 shadow-[inset_0_0_0_1px_var(--border-subtle)] hover:text-fg"
            >
              {l.name}
            </Link>
          ))}
        </div>
      ) : null}
      {campaigns?.map((c) => (
        <Link key={c.id} href={`/campaigns/${c.id}`} className="flex items-center gap-2 rounded-sm px-1 py-0.5 text-meta hover:bg-surface-2">
          <span className="min-w-0 flex-1 truncate text-fg-2">{c.name}</span>
          <span className="text-fg-3">found {relTime(c.first_discovered_at)}</span>
        </Link>
      ))}
    </Section>
  );
}

const FIELD_LABELS: Record<string, string> = {
  full_name: "Name",
  job_title: "Title",
  public_profile_url: "Profile",
  website_url: "Website",
  employee_count: "Employees",
  registry_id: "Registry ID",
  linkedin_url: "LinkedIn",
};

function SourcesTab({ groups }: { groups: { title: string; obs: Record<string, Observation[]> }[] }) {
  return (
    <div className="divide-y divide-line">
      {groups.map((g) => (
        <Section key={g.title} title={g.title}>
          {Object.keys(g.obs).length === 0 && <Empty>No recorded observations</Empty>}
          {Object.entries(g.obs).map(([field, list]) => (
            <div key={field} className="py-1">
              <div className="mb-1 text-meta font-medium text-fg-2">{FIELD_LABELS[field] ?? field.replace(/_/g, " ")}</div>
              <div className="space-y-1 border-l border-line pl-2.5">
                {list.map((o) => (
                  <div key={o.id} className="text-meta">
                    <div className="flex items-center gap-2">
                      <span className="min-w-0 flex-1 truncate text-fg">{formatValue(o.value)}</span>
                      {o.confidence != null && <span className="tabular text-fg-3">{Math.round(o.confidence * 100)}%</span>}
                    </div>
                    <div className="flex items-center gap-1.5 text-micro text-fg-3">
                      {o.source_url ? (
                        <a href={o.source_url} target="_blank" rel="noreferrer noopener" className="text-fg-2 hover:text-accent-strong hover:underline">
                          {o.source_label}
                        </a>
                      ) : (
                        <span className="text-fg-2">{o.source_label}</span>
                      )}
                      <span>· {shortDate(o.observed_at)}</span>
                    </div>
                    {o.evidence && <p className="mt-0.5 line-clamp-2 text-micro italic text-fg-3">“{o.evidence}”</p>}
                  </div>
                ))}
              </div>
            </div>
          ))}
        </Section>
      ))}
    </div>
  );
}

function HistoryTab({ entity, id }: { entity: "people" | "companies"; id: string }) {
  const q = useQuery({ queryKey: qk.history(entity, id), queryFn: () => api<HistoryEvent[]>(`${entity}/${id}/history`) });
  if (q.isLoading) {
    return (
      <div className="space-y-3 p-4">
        {Array.from({ length: 6 }).map((_, i) => (
          <Skeleton key={i} className="h-3" />
        ))}
      </div>
    );
  }
  const events = [...(q.data ?? [])].sort((a, b) => new Date(b.at).getTime() - new Date(a.at).getTime());
  if (!events.length)
    return (
      <div className="p-4">
        <Empty>No history yet</Empty>
      </div>
    );
  return (
    <ol className="relative space-y-3 px-4 py-4 before:absolute before:bottom-4 before:left-[21px] before:top-5 before:w-px before:bg-line">
      {events.map((e, i) => (
        <li key={`${e.type}-${e.at}-${i}`} className="relative flex gap-3">
          <span className={cn("relative z-[1] mt-1 size-2.5 shrink-0 rounded-full ring-4 ring-surface-1", dotFor(e.type))} />
          <div className="min-w-0 flex-1">
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-body text-fg">{e.label}</span>
              <span className="shrink-0 text-micro text-fg-3" title={new Date(e.at).toLocaleString()}>
                {relTime(e.at)}
              </span>
            </div>
            {e.detail && (
              <p className="truncate text-meta text-fg-3" title={e.detail}>
                {e.detail}
              </p>
            )}
            {e.source_url && (
              <a href={e.source_url} target="_blank" rel="noreferrer noopener" className="text-micro text-accent-strong hover:underline">
                {hostname(e.source_url)}
              </a>
            )}
          </div>
        </li>
      ))}
    </ol>
  );
}

function dotFor(type: string): string {
  if (type.includes("EXPORT")) return "bg-info";
  if (type.includes("EMAIL")) return "bg-success";
  if (type.includes("CANDIDATE")) return "bg-warning";
  if (type.includes("DISCOVER") || type.includes("FOUND")) return "bg-accent";
  return "bg-fg-3";
}
