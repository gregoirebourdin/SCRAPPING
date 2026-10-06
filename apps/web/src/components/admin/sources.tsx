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

function P({ v }: { v: number | null | undefined }) {
  return <>{v == null ? "—" : `${Math.round(v * 10) / 10}%`}</>;
}

/** Source health (spec §101–§102) and pipeline diagnostics. */
export function SourcesPage() {
  const me = useMe();
  const [hours, setHours] = useState("24");
  const sources = useQuery({ queryKey: qk.sources, queryFn: () => api<SourceRow[]>("sources"), refetchInterval: 30_000 });
  const diag = useQuery({ queryKey: ["diagnostics", hours], queryFn: () => api<Diagnostics>(`diagnostics?hours=${hours}`), refetchInterval: 30_000 });
  const d = diag.data;
  const f = me.data?.features ?? {};
  const all = (sources.data ?? []).filter((s) => s.key !== "fixture" || f.demo_data);
  const discovery = all.filter((s) => s.kind !== "evidence");
  const evidence = all.filter((s) => s.kind === "evidence");

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
