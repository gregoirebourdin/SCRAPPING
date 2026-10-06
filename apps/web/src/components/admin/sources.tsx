"use client";

import { Badge, cn, Segmented } from "@scout/design-system";
import { useQuery } from "@tanstack/react-query";
import { Database } from "lucide-react";
import { useState } from "react";

import { Block, DataTable, Metric, Page, Panel } from "@/components/common/page";
import { api } from "@/lib/api";
import { n, pct, relTime, usd } from "@/lib/format";
import { qk, useMe } from "@/lib/queries";

interface SourceRow {
  key: string;
  name: string;
  kind: string;
  description: string | null;
  quality: number | null;
  enabled: boolean;
  requests: number;
  success_rate: number | null;
  block_rate: number | null;
  avg_latency_ms: number | null;
  results_per_query: number | null;
  duplicate_rate: number | null;
  qualification_rate: number | null;
  unhealthy_until: string | null;
  last_error: string | null;
  last_success_at: string | null;
}

interface EmailMetrics {
  resolutions: number;
  by_path: { cache: number; fast: number; deep: number };
  by_status: Record<string, number>;
  avg_ms: number | null;
  p50_ms: number | null;
  p95_ms: number | null;
  smtp_probes: number;
  smtp_fallback_rate: number | null;
  cache_hit_rate: number | null;
  avg_candidates: number | null;
  resolved_per_minute: number | null;
  pending_deep: Record<string, number>;
  smtp_health: { scope: string; state: string; reason: string | null; blocked_until: string | null }[];
  resolvers: { resolver: string; attempts: number; confirmed_correct: number; confirmed_wrong: number; precision: number | null; avg_ms: number | null }[];
}

interface Diagnostics {
  window_hours: number;
  companies_per_min: number;
  pages_per_min: number;
  crawl_success_pct: number | null;
  browser_fallback_pct: number | null;
  people_found_pct: number | null;
  emails_found: number;
  safe_email_pct: number | null;
  catch_all_pct: number | null;
  qualification_pct: number | null;
  duplicates_pct: number | null;
  previously_seen_exclusions_pct: number | null;
  gemini_calls: number;
  grounded_searches: number;
  tokens: number;
  cost_usd: number;
  cost_per_qualified: number | null;
  job_failures: Record<string, number>;
}

/** One (dimension, key) of Empirical Source Scoring (GET /v1/learning/sources). */
interface ReliabilityRow {
  dimension: string;
  dimension_label: string;
  key: string;
  label: string;
  attempts: number;
  successes: number;
  coverage: number | null;
  confirmed_correct: number;
  confirmed_wrong: number;
  inconclusive: number;
  precision: number;
  raw_precision: number | null;
  precision_interval: [number, number] | null;
  effective_precision: number;
  effective_coverage: number;
  avg_latency_ms: number | null;
  avg_cost_usd: number | null;
  prior: number | null;
  prior_coverage: number | null;
  evidence_level: "prior only" | "learning" | "learned";
  last_outcome_at: string | null;
  updated_at: string | null;
}

const EVIDENCE_TONE: Record<ReliabilityRow["evidence_level"], "success" | "info" | "muted"> = { learned: "success", learning: "info", "prior only": "muted" };

function P({ v }: { v: number | null | undefined }) {
  return <>{v == null ? "—" : `${Math.round(v * 10) / 10}%`}</>;
}

/** Source health (spec §101–§102) and pipeline diagnostics. */
export function SourcesPage() {
  const me = useMe();
  const [hours, setHours] = useState("24");
  const sources = useQuery({ queryKey: qk.sources, queryFn: () => api<SourceRow[]>("sources"), refetchInterval: 30_000 });
  const diag = useQuery({ queryKey: ["diagnostics", hours], queryFn: () => api<Diagnostics>(`diagnostics?hours=${hours}`), refetchInterval: 30_000 });
  const em = useQuery({ queryKey: ["email-metrics", hours], queryFn: () => api<EmailMetrics>(`email/metrics?hours=${hours}`), refetchInterval: 30_000 });
  const ws = me.data?.workspaces.find((w) => w.id === me.data?.current_workspace_id) ?? me.data?.workspaces[0];
  const isAdmin = ws?.role === "owner" || ws?.role === "admin";
  const reliability = useQuery({
    queryKey: ["learning-sources"],
    queryFn: () => api<ReliabilityRow[]>("learning/sources"),
    refetchInterval: 60_000,
    enabled: isAdmin,
  });
  const e = em.data;
  const d = diag.data;
  const f = me.data?.features ?? {};
  const all = (sources.data ?? []).filter((s) => s.key !== "fixture" || f.demo_data);
  const discovery = all.filter((s) => s.kind !== "evidence");
  const evidence = all.filter((s) => s.kind === "evidence");
  const reliabilityGroups: { dimension: string; label: string; rows: ReliabilityRow[] }[] = [];
  for (const r of reliability.data ?? []) {
    if (r.key === "fixture" && !f.demo_data) continue;
    const g = reliabilityGroups.find((x) => x.dimension === r.dimension);
    if (g) g.rows.push(r);
    else reliabilityGroups.push({ dimension: r.dimension, label: r.dimension_label, rows: [r] });
  }

  return (
    <Page
      title="Sources & health"
      icon={<Database className="size-4 text-fg-3" />}
      actions={
        <Segmented
          value={hours}
          onChange={setHours}
          options={[
            { value: "1", label: "1h" },
            { value: "24", label: "24h" },
            { value: "168", label: "7d" },
          ]}
        />
      }
    >
      <Block title="Pipeline">
        <div className="grid grid-cols-2 gap-2 md:grid-cols-4 lg:grid-cols-6">
          <Metric label="Companies / min" value={d ? d.companies_per_min.toFixed(1) : "—"} />
          <Metric label="Pages / min" value={d ? d.pages_per_min.toFixed(1) : "—"} />
          <Metric
            label="Crawl success"
            value={<P v={d?.crawl_success_pct} />}
            hint={d?.browser_fallback_pct != null ? `${Math.round(d.browser_fallback_pct)}% needed a browser` : undefined}
          />
          <Metric label="People found" value={<P v={d?.people_found_pct} />} hint="of evaluated companies" />
          <Metric
            label="Safe emails"
            value={<P v={d?.safe_email_pct} />}
            hint={`${n(d?.emails_found)} emails · ${d?.catch_all_pct != null ? Math.round(d.catch_all_pct) : "—"}% catch-all`}
          />
          <Metric
            label="Qualified"
            value={<P v={d?.qualification_pct} />}
            hint={`${d?.duplicates_pct != null ? Math.round(d.duplicates_pct) : "—"}% dup · ${d?.previously_seen_exclusions_pct != null ? Math.round(d.previously_seen_exclusions_pct) : "—"}% known`}
          />
          <Metric label="AI calls" value={n(d?.gemini_calls)} hint={`${n(d?.grounded_searches)} grounded searches`} />
          <Metric label="Tokens" value={n(d?.tokens)} />
          <Metric label="AI cost" value={usd(d?.cost_usd)} hint={d?.cost_per_qualified ? `${usd(d.cost_per_qualified, 3)} / qualified` : undefined} />
          <Metric
            label="Job failures"
            value={n(Object.values(d?.job_failures ?? {}).reduce((a, b) => a + b, 0))}
            tone={Object.keys(d?.job_failures ?? {}).length ? "warning" : undefined}
            hint={
              Object.entries(d?.job_failures ?? {})
                .slice(0, 2)
                .map(([k, v]) => `${k}: ${v}`)
                .join(" · ") || undefined
            }
          />
        </div>
      </Block>

      <Block title="Email engine" aside={<span className="text-meta text-fg-3">Domain intelligence → fast path; SMTP only for ambiguous cases</span>}>
        <div className="grid grid-cols-2 gap-2 md:grid-cols-4 lg:grid-cols-6">
          <Metric label="Resolutions" value={n(e?.resolutions)} hint={e ? `${n(e.by_path.cache)} cache · ${n(e.by_path.fast)} fast · ${n(e.by_path.deep)} deep` : undefined} />
          <Metric label="Resolved / min" value={e?.resolved_per_minute != null ? e.resolved_per_minute.toFixed(1) : "—"} hint="SAFE + likely safe" />
          <Metric label="P50 / P95" value={e?.p50_ms != null ? `${Math.round(e.p50_ms)} / ${Math.round(e.p95_ms ?? 0)} ms` : "—"} />
          <Metric label="Cache hit rate" value={e?.cache_hit_rate != null ? pct(e.cache_hit_rate) : "—"} hint="email or domain profile reused" />
          <Metric
            label="SMTP fallback"
            value={e?.smtp_fallback_rate != null ? pct(e.smtp_fallback_rate) : "—"}
            hint={`${n(e?.smtp_probes)} probes · ${e?.avg_candidates ?? "—"} cand./person`}
          />
          <Metric
            label="SMTP health"
            value={e?.smtp_health.find((h) => h.scope === "global")?.state ?? "UNKNOWN"}
            tone={e?.smtp_health.some((h) => h.state === "BLOCKED") ? "danger" : e?.smtp_health.some((h) => h.state === "DEGRADED") ? "warning" : undefined}
            hint={
              e?.smtp_health
                .filter((h) => h.state !== "HEALTHY" && h.scope !== "global")
                .map((h) => `${h.scope.replace("provider:", "")}: ${h.state}`)
                .join(" · ") || undefined
            }
          />
        </div>
        {e && Object.keys(e.by_status).length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {Object.entries(e.by_status).map(([k, v]) => (
              <Badge key={k} tone="neutral">
                {k.replace(/_/g, " ").toLowerCase()} · {n(v)}
              </Badge>
            ))}
            {Object.entries(e.pending_deep).map(([k, v]) => (
              <Badge key={k} tone="info" dot>
                deep {k} · {n(v)}
              </Badge>
            ))}
          </div>
        )}
        {e && e.resolvers.length > 0 && (
          <div className="mt-3">
            <DataTable<EmailMetrics["resolvers"][number]>
              rows={e.resolvers}
              rowKey={(r) => r.resolver}
              columns={[
                { key: "r", label: "Resolver", render: (r) => <span className="text-fg">{r.resolver.replace(/_/g, " ")}</span> },
                { key: "a", label: "Attempts", className: "text-right", render: (r) => <span className="tabular">{n(r.attempts)}</span> },
                { key: "c", label: "Confirmed", className: "text-right", render: (r) => <span className="tabular text-success">{n(r.confirmed_correct)}</span> },
                { key: "w", label: "Wrong", className: "text-right", render: (r) => <span className="tabular text-danger">{n(r.confirmed_wrong)}</span> },
                {
                  key: "p",
                  label: "Precision (smoothed)",
                  className: "text-right",
                  render: (r) => <span className="tabular">{r.precision != null ? pct(r.precision, 1) : "—"}</span>,
                },
                {
                  key: "ms",
                  label: "Avg time",
                  className: "text-right",
                  render: (r) => <span className="tabular text-fg-2">{r.avg_ms != null ? `${Math.round(r.avg_ms)} ms` : "—"}</span>,
                },
              ]}
            />
          </div>
        )}
      </Block>

      <Block title="Capabilities">
        <div className="flex flex-wrap gap-1.5">
          {[
            ["Google Maps scraper", f.maps],
            ["SMTP verification", f.smtp_verification],
            ["Verifier service", f.verifier_service],
            ["Grounded web research", f.grounded_search],
            ["Browser rendering", f.browser_rendering],
          ].map(([label, on]) => (
            <Badge key={String(label)} tone={on ? "success" : "muted"} dot>
              {String(label)}
              {on ? "" : " · off"}
            </Badge>
          ))}
        </div>
        {!f.smtp_verification && (
          <p className="mt-2 text-meta text-fg-3">
            Without SMTP verification, emails are graded from MX records, published addresses and confirmed patterns; unconfirmed guesses stay “Risky” rather than “Safe”.
          </p>
        )}
      </Block>

      <Block title="Discovery sources">
        <DataTable<SourceRow>
          rows={discovery}
          loading={sources.isLoading}
          rowKey={(s) => s.key}
          empty="No source has run yet"
          columns={[
            {
              key: "name",
              label: "Source",
              render: (s) => (
                <span className="flex flex-col">
                  <span className="text-fg">{s.name}</span>
                  {s.description && <span className="line-clamp-1 text-micro text-fg-3">{s.description}</span>}
                </span>
              ),
            },
            { key: "kind", label: "Kind", render: (s) => <span className="text-fg-2">{s.kind.replace(/_/g, " ")}</span> },
            {
              key: "health",
              label: "Health",
              render: (s) => {
                const down = s.unhealthy_until && new Date(s.unhealthy_until) > new Date();
                return (
                  <span title={s.last_error ?? undefined}>
                    <Badge tone={!s.enabled ? "muted" : down ? "danger" : s.requests ? "success" : "neutral"} dot>
                      {!s.enabled ? "disabled" : down ? `paused · ${relTime(s.unhealthy_until)}` : s.requests ? "healthy" : "idle"}
                    </Badge>
                  </span>
                );
              },
            },
            { key: "req", label: "Requests", className: "text-right", render: (s) => <span className="tabular">{n(s.requests)}</span> },
            {
              key: "ok",
              label: "Success",
              className: "text-right",
              render: (s) => <span className={cn("tabular", (s.success_rate ?? 1) < 0.8 && "text-warning")}>{pct(s.success_rate)}</span>,
            },
            {
              key: "block",
              label: "Blocked",
              className: "text-right",
              render: (s) => <span className={cn("tabular", (s.block_rate ?? 0) > 0.1 && "text-danger")}>{pct(s.block_rate)}</span>,
            },
            {
              key: "lat",
              label: "Latency",
              className: "text-right",
              render: (s) => <span className="tabular text-fg-2">{s.avg_latency_ms != null ? `${n(s.avg_latency_ms)} ms` : "—"}</span>,
            },
            { key: "rpq", label: "Results / query", className: "text-right", render: (s) => <span className="tabular text-fg-2">{s.results_per_query ?? "—"}</span> },
            { key: "dup", label: "Duplicates", className: "text-right", render: (s) => <span className="tabular text-fg-2">{pct(s.duplicate_rate)}</span> },
            { key: "q", label: "Qualified", className: "text-right", render: (s) => <span className="tabular text-fg">{pct(s.qualification_rate, 1)}</span> },
            { key: "last", label: "Last success", className: "text-right", render: (s) => <span className="text-fg-3">{relTime(s.last_success_at)}</span> },
          ]}
        />
      </Block>

      {isAdmin && (
        <Block
          title="Source reliability"
          aside={
            <span className="text-meta text-fg-3">Learned from confirmed outcomes — drives source routing, confidence and enrichment planning; priors until enough evidence</span>
          }
        >
          {reliability.isLoading && <DataTable<ReliabilityRow> rows={[]} loading rowKey={(r) => r.key} columns={reliabilityColumns} />}
          {reliability.isError && <p className="text-meta text-fg-3">Source reliability is unavailable right now.</p>}
          <div className="flex flex-col gap-4">
            {reliabilityGroups.map((g) => (
              <div key={g.dimension} className="flex flex-col gap-1.5">
                <div className="flex items-baseline gap-2">
                  <span className="text-body text-fg">{g.label}</span>
                  <span className="text-micro text-fg-3">{g.dimension}</span>
                </div>
                <DataTable<ReliabilityRow> rows={g.rows} rowKey={(r) => `${r.dimension}:${r.key}`} columns={reliabilityColumns} />
              </div>
            ))}
          </div>
        </Block>
      )}

      {evidence.length > 0 && (
        <Block title="Evidence sources" aside={<span className="text-meta text-fg-3">Where field values come from — shown in every source inspector</span>}>
          <Panel className="divide-y divide-line">
            {evidence.map((s) => (
              <div key={s.key} className="flex items-center gap-3 px-3 py-2 text-body">
                <span className="w-48 shrink-0 text-fg">{s.name}</span>
                <span className="min-w-0 flex-1 truncate text-meta text-fg-3">{s.description}</span>
                {s.quality != null && <span className="tabular text-meta text-fg-2">trust {Math.round(s.quality * 100)}%</span>}
              </div>
            ))}
          </Panel>
        </Block>
      )}
    </Page>
  );
}

const reliabilityColumns: { key: string; label: React.ReactNode; className?: string; render: (r: ReliabilityRow) => React.ReactNode }[] = [
  {
    key: "source",
    label: "Source",
    render: (r) => (
      <span className="flex flex-col">
        <span className="text-fg">{r.label}</span>
        <span className="text-micro text-fg-3">{r.key}</span>
      </span>
    ),
  },
  { key: "attempts", label: "Attempts", className: "text-right", render: (r) => <span className="tabular">{n(r.attempts)}</span> },
  {
    key: "coverage",
    label: "Coverage",
    className: "text-right",
    render: (r) => (
      <span className="tabular text-fg-2" title={r.prior_coverage != null ? `Prior ${pct(r.prior_coverage)} · ${n(r.successes)} usable results` : undefined}>
        {pct(r.coverage)}
      </span>
    ),
  },
  {
    key: "precision",
    label: "Precision (90% CI)",
    className: "text-right",
    render: (r) => (
      <span
        className="flex flex-col items-end"
        title={`${n(r.confirmed_correct)} confirmed correct · ${n(r.confirmed_wrong)} wrong${r.prior != null ? ` · prior ${pct(r.prior)}` : ""} · used: ${pct(r.effective_precision, 1)}`}
      >
        <span className={cn("tabular", r.confirmed_correct + r.confirmed_wrong ? "text-fg" : "text-fg-3")}>{pct(r.precision, 1)}</span>
        {r.precision_interval && (
          <span className="tabular text-micro text-fg-3">
            {pct(r.precision_interval[0])}–{pct(r.precision_interval[1])}
          </span>
        )}
      </span>
    ),
  },
  {
    key: "judged",
    label: "Confirmed",
    className: "text-right",
    render: (r) => (
      <span className="tabular">
        <span className="text-success">{n(r.confirmed_correct)}</span>
        <span className="text-fg-3"> / </span>
        <span className={cn(r.confirmed_wrong ? "text-danger" : "text-fg-3")}>{n(r.confirmed_wrong)}</span>
      </span>
    ),
  },
  {
    key: "latency",
    label: "Latency",
    className: "text-right",
    render: (r) => <span className="tabular text-fg-2">{r.avg_latency_ms != null ? `${n(Math.round(r.avg_latency_ms))} ms` : "—"}</span>,
  },
  {
    key: "cost",
    label: "Cost / attempt",
    className: "text-right",
    render: (r) => <span className="tabular text-fg-2">{r.avg_cost_usd == null ? "—" : r.avg_cost_usd > 0 ? usd(r.avg_cost_usd, 4) : "free"}</span>,
  },
  {
    key: "evidence",
    label: "Evidence",
    className: "text-right",
    render: (r) => (
      <Badge tone={EVIDENCE_TONE[r.evidence_level]} dot>
        {r.evidence_level}
      </Badge>
    ),
  },
];
