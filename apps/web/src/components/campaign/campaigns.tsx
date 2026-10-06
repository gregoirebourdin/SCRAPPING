"use client";

import { Badge, Button, cn, Dialog, DialogContent, Input, ProgressBar, Spinner, Tip } from "@scout/design-system";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Bookmark, CircleAlert, Radar, RotateCcw } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { toast } from "sonner";

import { StatusText } from "@/components/chat/tool-cards";
import { Block, DataTable, Metric, Page, Panel } from "@/components/common/page";
import { useLive } from "@/components/shell/live-events";

import { RunHeader } from "./run-header";
import { TableView } from "@/components/table/table-view";
import type { TableScope } from "@/components/table/use-rows";
import { api } from "@/lib/api";
import { n, pct, relTime, shortDate, sourceLabel, usd } from "@/lib/format";
import { type CampaignStatus, type CampaignSummary, qk, useCampaign, useCampaigns } from "@/lib/queries";

const ACTIVE = ["running", "planning"];

export function CampaignsIndex() {
  const q = useCampaigns();
  const router = useRouter();
  const live = useLive((s) => s.campaigns);
  return (
    <Page
      title="Campaigns"
      icon={<Radar className="size-4 text-fg-3" />}
      actions={
        <Link href="/discover" className="inline-flex h-7 items-center gap-1.5 rounded-sm bg-accent px-2.5 text-body font-medium text-accent-contrast hover:bg-accent-strong">
          New search
        </Link>
      }
    >
      <DataTable<CampaignSummary>
        rows={q.data}
        loading={q.isLoading}
        rowKey={(c) => c.id}
        onRowClick={(c) => router.push(`/campaigns/${c.id}`)}
        empty="No searches yet. Describe who you want to find on the Discover page."
        columns={[
          { key: "name", label: "Search", render: (c) => <span className="font-medium text-fg">{c.name}</span> },
          { key: "status", label: "Status", render: (c) => <StatusText status={live[c.id]?.status ?? c.status} /> },
          {
            key: "progress",
            label: "Qualified",
            render: (c) => {
              const qual = live[c.id]?.qualified ?? c.qualified;
              return (
                <span className="flex items-center gap-2">
                  <span className="tabular w-24 text-fg">
                    {n(qual)} / {n(c.target)}
                  </span>
                  <ProgressBar value={qual} max={Math.max(1, c.target)} className="w-20" tone={c.status === "completed" ? "success" : "accent"} />
                </span>
              );
            },
          },
          { key: "raw", label: "Discovered", className: "text-right", render: (c) => <span className="tabular text-fg-2">{n(live[c.id]?.raw ?? c.raw)}</span> },
          { key: "yield", label: "Yield", className: "text-right", render: (c) => <span className="tabular text-fg-2">{c.raw ? pct(c.qualified / c.raw, 1) : "—"}</span> },
          { key: "cost", label: "Cost", className: "text-right", render: (c) => <span className="tabular text-fg-2">{usd(c.cost_usd)}</span> },
          { key: "created", label: "Started", className: "text-right", render: (c) => <span className="text-fg-3">{relTime(c.created_at)}</span> },
        ]}
      />
    </Page>
  );
}

interface Rejected {
  id: string;
  name: string | null;
  domain: string | null;
  source: string;
  outcome: string;
  reason: string | null;
  stage: string | null;
  company_id: string | null;
  observed_at: string;
}

export function CampaignDetail({ id }: { id: string }) {
  const q = useCampaign(id);
  const live = useLive((s) => s.campaigns[id]);
  const qc = useQueryClient();
  const router = useRouter();
  const [showRejected, setShowRejected] = useState(false);
  const [templateOpen, setTemplateOpen] = useState(false);
  const [templateName, setTemplateName] = useState("");
  const scope = useMemo<TableScope>(() => ({ key: `campaign:${id}`, kind: "campaign", listId: null, campaignId: id, entityType: "person" }), [id]);

  if (q.isLoading)
    return (
      <div className="grid flex-1 place-items-center">
        <Spinner size={16} />
      </div>
    );
  if (q.isError || !q.data) {
    return (
      <Page title="Campaign">
        <p className="text-body text-fg-3">{(q.error as Error)?.message ?? "Not found"}</p>
      </Page>
    );
  }
  const c: CampaignStatus = q.data;
  const st = { ...c.stats, ...(live ?? {}) } as Record<string, number>;
  const status = live?.status ?? c.status;
  const qualified = live?.qualified ?? c.stats.qualified ?? 0;
  const rate = live?.rate_per_minute ?? c.rate_per_minute;
  const eta = live?.eta_minutes ?? c.eta_minutes;

  async function runAgain() {
    try {
      const t = await api<{ id: string }>("campaign-templates", { body: { name: `${c.name} (rerun ${new Date().toLocaleDateString()})`, campaign_id: id } });
      const nc = await api<CampaignStatus>(`campaign-templates/${t.id}/run`, { body: { only_new: true } });
      toast.success("Started a new run — only leads you have never seen");
      void qc.invalidateQueries({ queryKey: qk.campaigns });
      router.push(`/campaigns/${nc.id}`);
    } catch (e) {
      toast.error((e as Error).message);
    }
  }

  const funnel: [string, number | undefined, string?][] = [
    ["Discovered", st.raw_discovered ?? live?.raw],
    ["New companies", st.unique_new_companies, "after registry + exclusions"],
    ["Evaluated", st.companies_evaluated ?? live?.evaluated],
    ["Matched ICP", st.companies_matched],
    ["People found", st.people_found ?? live?.people],
    ["Emails found", st.emails_found ?? live?.emails],
    ["Safe emails", st.emails_safe ?? live?.safe],
    ["Qualified", qualified],
  ];
  const maxFunnel = Math.max(1, ...funnel.map(([, v]) => v ?? 0));

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <header className="flex h-12 shrink-0 items-center gap-2 border-b border-line px-4">
        <Link href="/campaigns" className="rounded-sm p-1 text-fg-3 hover:bg-surface-2 hover:text-fg" aria-label="All campaigns">
          <ArrowLeft className="size-4" />
        </Link>
        <h1 className="title-gradient truncate text-title tracking-[-0.01em]">{c.name}</h1>
        <StatusText status={status} />
        <div className="ml-auto flex items-center gap-1">
          {status === "cancelled" && (
            <Tip content="Run the same search again — only new leads">
              <Button size="sm" variant="ghost" onClick={() => void runAgain()}>
                <RotateCcw /> Run again
              </Button>
            </Tip>
          )}
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setTemplateName(c.name);
              setTemplateOpen(true);
            }}
          >
            <Bookmark /> Save search
          </Button>
          {c.target_list_id && (
            <Link
              href={`/lists/${c.target_list_id}`}
              className="inline-flex h-7 items-center rounded-sm bg-surface-2 px-2.5 text-body text-fg shadow-[inset_0_0_0_1px_var(--border-strong)] hover:bg-surface-3"
            >
              Open list
            </Link>
          )}
        </div>
      </header>
      <RunHeader campaignId={id} />
      <div className="max-h-[52%] shrink-0 overflow-y-auto border-b border-line">
        <div className="grid gap-4 px-4 py-4 lg:grid-cols-[1fr_340px]">
          <div className="min-w-0 space-y-4">
            <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
              <Metric
                label="Qualified"
                value={`${n(qualified)} / ${n(c.target)}`}
                hint={<ProgressBar value={qualified} max={Math.max(1, c.target)} className="mt-1" tone={status === "completed" ? "success" : "accent"} />}
              />
              <Metric label="Rate" value={rate ? `${rate}/min` : "—"} hint={eta && ACTIVE.includes(status) ? `~${eta} min left` : rate ? "rolling average" : "measuring…"} />
              <Metric label="Cost" value={usd(live?.cost_usd ?? c.cost_usd)} hint={c.cost_per_qualified ? `${usd(c.cost_per_qualified, 3)} / qualified` : undefined} />
              <Metric
                label="Excluded as known"
                value={n(st.excluded_previous ?? live?.excluded)}
                hint={`${n(st.duplicates ?? live?.duplicates)} duplicates · ${n(st.suppressed)} suppressed`}
              />
            </div>
            {(c.stop_reason || live?.reason) && !ACTIVE.includes(status) && (
              <Panel className="flex items-start gap-2 px-3 py-2 text-body text-fg-2">
                <CircleAlert className="mt-0.5 size-4 shrink-0 text-fg-3" />
                {live?.reason ?? c.stop_reason}
              </Panel>
            )}
            {c.sources_exhausting && ACTIVE.includes(status) && (
              <Panel className="flex items-start gap-2 px-3 py-2 text-body text-warning">
                <CircleAlert className="mt-0.5 size-4 shrink-0" />
                Sources are running out of new candidates. Consider widening the location, size or industry.
              </Panel>
            )}
            <Block title="Funnel">
              <Panel className="space-y-1.5 px-3 py-3">
                {funnel.map(([label, v, hint]) => (
                  <div key={label} className="grid grid-cols-[120px_1fr_64px] items-center gap-3 text-meta">
                    <span className="text-fg-2" title={hint}>
                      {label}
                    </span>
                    <span className="h-1.5 overflow-hidden rounded-full bg-surface-3">
                      <span
                        className={cn("block h-full rounded-full", label === "Qualified" ? "bg-success" : "bg-accent/70")}
                        style={{ width: `${((v ?? 0) / maxFunnel) * 100}%` }}
                      />
                    </span>
                    <span className="tabular text-right text-fg">{n(v ?? 0)}</span>
                  </div>
                ))}
              </Panel>
            </Block>
            <Block title="Sources">
              <DataTable
                rows={c.sources}
                rowKey={(s) => s.key}
                empty="No sources planned yet"
                columns={[
                  { key: "src", label: "Source", render: (s) => <span className="text-fg">{sourceLabel(s.key)}</span> },
                  {
                    key: "status",
                    label: "Status",
                    render: (s) => (
                      <Badge tone={s.status === "active" ? "info" : s.status === "exhausted" ? "muted" : s.status === "unhealthy" ? "danger" : "neutral"}>{s.status}</Badge>
                    ),
                  },
                  { key: "raw", label: "Raw", className: "text-right", render: (s) => <span className="tabular">{n(s.raw)}</span> },
                  { key: "unique", label: "New", className: "text-right", render: (s) => <span className="tabular">{n(s.unique)}</span> },
                  { key: "q", label: "Qualified", className: "text-right", render: (s) => <span className="tabular">{n(s.qualified)}</span> },
                  {
                    key: "err",
                    label: "Errors",
                    className: "text-right",
                    render: (s) => (
                      <span className={cn("tabular", s.errors ? "text-warning" : "text-fg-3")} title={s.last_error ?? undefined}>
                        {n(s.errors)}
                      </span>
                    ),
                  },
                ]}
              />
            </Block>
          </div>
          <div className="min-w-0 space-y-4">
            <Block title="Interpretation">
              <Panel className="px-3 py-2.5">
                {c.prompt && <p className="mb-2 text-meta italic text-fg-3">“{c.prompt}”</p>}
                <dl className="grid grid-cols-[96px_1fr] gap-x-3 gap-y-1 text-meta">
                  {c.interpretation.map((i) => (
                    <div key={`${i.label}:${i.value}`} className="contents">
                      <dt className="text-fg-3">{i.label}</dt>
                      <dd className="text-fg-2">{i.value}</dd>
                    </div>
                  ))}
                </dl>
                <p className="mt-2 text-micro text-fg-3">
                  Started {shortDate(c.started_at ?? c.created_at)}
                  {c.stopped_at ? ` · stopped ${relTime(c.stopped_at)}` : ""}
                </p>
              </Panel>
            </Block>
            <Block
              title="Why leads were rejected"
              aside={
                <button type="button" className="text-meta text-accent-strong hover:underline" onClick={() => setShowRejected(true)}>
                  View rejected
                </button>
              }
            >
              <Panel className="space-y-1 px-3 py-2.5">
                {c.top_rejections.length === 0 && <p className="text-meta text-fg-3">No rejections yet</p>}
                {c.top_rejections.map((r) => (
                  <div key={r.reason} className="flex items-center justify-between gap-2 text-meta">
                    <span className="truncate text-fg-2">{r.reason.replace(/_/g, " ")}</span>
                    <span className="tabular text-fg">{n(r.count)}</span>
                  </div>
                ))}
              </Panel>
            </Block>
          </div>
        </div>
      </div>
      <TableView
        scope={scope}
        title="Qualified leads"
        emptyState={
          <p className="text-meta text-fg-3">{ACTIVE.includes(status) ? "Leads appear here as soon as they pass the quality gate." : "No qualified leads in this run."}</p>
        }
      />
      <RejectedDialog id={id} open={showRejected} onOpenChange={setShowRejected} />
      <Dialog open={templateOpen} onOpenChange={setTemplateOpen}>
        <DialogContent title="Save search" description="Re-run it later — reruns only return leads you have never seen." width={420}>
          <form
            className="space-y-3 px-4 py-3"
            onSubmit={async (e) => {
              e.preventDefault();
              setTemplateOpen(false);
              try {
                await api("campaign-templates", { body: { name: templateName.trim(), campaign_id: id } });
                toast.success("Search saved", { description: "Find it in Settings → Saved searches" });
              } catch (err) {
                toast.error((err as Error).message);
              }
            }}
          >
            <Input autoFocus value={templateName} onChange={(e) => setTemplateName(e.target.value)} aria-label="Search name" />
            <div className="flex justify-end gap-2">
              <Button variant="ghost" onClick={() => setTemplateOpen(false)}>
                Cancel
              </Button>
              <Button type="submit" variant="primary" disabled={!templateName.trim()}>
                Save
              </Button>
            </div>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function RejectedDialog({ id, open, onOpenChange }: { id: string; open: boolean; onOpenChange: (v: boolean) => void }) {
  const q = useQuery({ queryKey: ["rejections", id], queryFn: () => api<Rejected[]>(`campaigns/${id}/rejections?limit=500`), enabled: open });
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent title="Rejected candidates" description="Every candidate keeps its reason — nothing is silently dropped." width={760}>
        <div className="max-h-[60vh] overflow-y-auto p-3">
          <DataTable<Rejected>
            rows={q.data}
            loading={q.isLoading}
            rowKey={(r) => r.id}
            empty="No rejected candidates"
            columns={[
              { key: "name", label: "Company", render: (r) => <span className="text-fg">{r.name ?? r.domain ?? "—"}</span> },
              { key: "domain", label: "Domain", render: (r) => <span className="text-fg-3">{r.domain ?? ""}</span> },
              {
                key: "outcome",
                label: "Outcome",
                render: (r) => <Badge tone={r.outcome.includes("excluded") || r.outcome === "duplicate" ? "muted" : "warning"}>{r.outcome.replace(/_/g, " ")}</Badge>,
              },
              {
                key: "reason",
                label: "Reason",
                render: (r) => (
                  <span className="line-clamp-1 text-fg-2" title={r.reason ?? ""}>
                    {r.reason ?? "—"}
                  </span>
                ),
              },
              { key: "src", label: "Source", render: (r) => <span className="text-fg-3">{sourceLabel(r.source)}</span> },
            ]}
          />
        </div>
      </DialogContent>
    </Dialog>
  );
}
