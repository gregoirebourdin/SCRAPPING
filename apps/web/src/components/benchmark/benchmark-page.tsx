"use client";

import { Badge, Button, Checkbox, Dialog, IconButton, ProgressBar } from "@scout/design-system";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { FlaskConical, GitCompareArrows, Play, Plus, ShieldAlert, Trash2 } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import { Block, DataTable, Page, Panel } from "@/components/common/page";
import { api, type ApiError } from "@/lib/api";
import { n, relTime, usd } from "@/lib/format";

import { ImportDatasetFlow } from "./import-dialog";
import { ComparisonTable, MeasurementNote } from "./metrics";
import { RunLauncher } from "./run-dialog";
import { type Comparison, type Dataset, fmtMetric, type Run, type RunMode, runTarget, STATUS_TONE } from "./types";

export const benchmarkKeys = {
  datasets: ["benchmark", "datasets"] as const,
  runs: ["benchmark", "runs"] as const,
  run: (id: string) => ["benchmark", "run", id] as const,
};

function Forbidden() {
  return (
    <Panel className="flex items-start gap-3 px-4 py-5">
      <ShieldAlert className="mt-0.5 size-4 shrink-0 text-fg-3" />
      <div>
        <p className="text-body text-fg">The benchmark harness is restricted to workspace admins.</p>
        <p className="mt-0.5 text-meta text-fg-3">Ask an owner to grant you the admin role.</p>
      </div>
    </Panel>
  );
}

function Headline({ r }: { r: Run }) {
  const parts = Object.values(r.headline).filter(Boolean);
  if (!parts.length) return <span className="text-fg-3">—</span>;
  return (
    <span className="flex flex-wrap gap-x-3 gap-y-0.5">
      {parts.slice(0, 3).map((m) => (
        <span key={m!.label} className="whitespace-nowrap" title={`${m!.label}: ${m!.k ?? "?"}/${m!.n ?? 0}`}>
          <span className="text-fg-3">{m!.label.replace(" precision", " P").replace(" recall", " R").replace(" (deliverable)", "")} </span>
          <span className="tabular text-fg">{fmtMetric(m!.value, m!.unit)}</span>
        </span>
      ))}
    </span>
  );
}

/** Internal Benchmark Harness: ground-truth datasets, runs (registry / live / suite), comparison. */
export function BenchmarkPage() {
  const qc = useQueryClient();
  const router = useRouter();
  const [importOpen, setImportOpen] = useState(false);
  const [launch, setLaunch] = useState<{ dataset?: string; mode?: RunMode } | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const datasets = useQuery({ queryKey: benchmarkKeys.datasets, queryFn: () => api<Dataset[]>("benchmark/datasets"), retry: (c, e) => (e as ApiError).status !== 403 && c < 2 });
  const runs = useQuery({
    queryKey: benchmarkKeys.runs,
    queryFn: () => api<Run[]>("benchmark/runs?limit=100"),
    retry: (c, e) => (e as ApiError).status !== 403 && c < 2,
    refetchInterval: (q) => (q.state.data?.some((r) => r.status === "queued" || r.status === "running") ? 2_000 : false),
  });
  const compare = useQuery({
    queryKey: ["benchmark", "compare", selected.join(",")],
    queryFn: () => api<Comparison>(`benchmark/compare?ids=${selected.join(",")}`),
    enabled: selected.length >= 2,
  });
  const forbidden = (datasets.error as ApiError | null)?.status === 403;

  async function removeDataset(d: Dataset) {
    if (!window.confirm(`Delete “${d.name}” and its ${d.item_count} items? Past runs keep their results.`)) return;
    try {
      await api(`benchmark/datasets/${d.id}`, { method: "DELETE" });
      void qc.invalidateQueries({ queryKey: ["benchmark"] });
      toast("Dataset deleted");
    } catch (e) {
      toast.error((e as Error).message);
    }
  }

  const toggle = (id: string, on: boolean) => setSelected((s) => (on ? [...s.filter((x) => x !== id), id].slice(-6) : s.filter((x) => x !== id)));

  return (
    <Page
      title="Benchmark"
      icon={<FlaskConical className="size-4 text-fg-3" />}
      actions={
        !forbidden && (
          <>
            <Button variant="ghost" onClick={() => setImportOpen(true)} aria-label="Import ground truth">
              <Plus /> <span className="hidden sm:inline">Import ground truth</span>
            </Button>
            <Button variant="primary" onClick={() => setLaunch({})}>
              <Play /> <span className="hidden sm:inline">New run</span>
              <span className="sm:hidden">Run</span>
            </Button>
          </>
        )
      }
    >
      <MeasurementNote className="mb-5 max-w-3xl" />
      {forbidden ? (
        <Forbidden />
      ) : (
        <>
          <Block title="Ground-truth datasets" aside={<span className="hidden text-meta text-fg-3 sm:inline">Expected values you verified — never generated</span>}>
            <DataTable<Dataset>
              rows={datasets.data}
              loading={datasets.isLoading}
              rowKey={(d) => d.id}
              empty={
                <span>
                  No dataset yet —{" "}
                  <button type="button" className="text-accent-strong hover:underline" onClick={() => setImportOpen(true)}>
                    import a CSV
                  </button>{" "}
                  or try the demo suite.
                </span>
              }
              columns={[
                {
                  key: "name",
                  label: "Dataset",
                  render: (d) => (
                    <span className="flex min-w-0 flex-col">
                      <span className="truncate text-fg">{d.name}</span>
                      <span className="text-micro text-fg-3">
                        {d.company_input === "name" ? "name-only input" : "domain input"}
                        {d.people_exhaustive ? "" : " · partial people"}
                      </span>
                    </span>
                  ),
                },
                { key: "kind", label: "Kind", render: (d) => <Badge tone="neutral">{d.kind}</Badge> },
                {
                  key: "items",
                  label: "Truth",
                  render: (d) => (
                    <span className="whitespace-nowrap text-fg-2">
                      {n(d.item_count)} co · {n(d.summary.people ?? 0)} ppl · {n((d.summary.emails ?? 0) + (d.summary.invalid_emails ?? 0))} emails
                      {d.summary.enrichment_values ? ` · ${n(d.summary.enrichment_values)} values` : ""}
                    </span>
                  ),
                },
                { key: "runs", label: "Runs", className: "text-right", render: (d) => <span className="tabular text-fg-2">{n(d.runs)}</span> },
                { key: "at", label: "Imported", className: "whitespace-nowrap", render: (d) => <span className="text-fg-3">{relTime(d.created_at)}</span> },
                {
                  key: "act",
                  label: "",
                  className: "w-px text-right",
                  render: (d) => (
                    <span className="flex justify-end gap-1">
                      <Button size="xs" variant="secondary" onClick={() => setLaunch({ dataset: d.id, mode: "registry" })}>
                        Run
                      </Button>
                      <IconButton size="xs" label={`Delete ${d.name}`} onClick={() => void removeDataset(d)}>
                        <Trash2 />
                      </IconButton>
                    </span>
                  ),
                },
              ]}
            />
          </Block>

          <Block
            title="Runs"
            aside={
              <span className="flex items-center gap-2 text-meta text-fg-3">
                <GitCompareArrows className="size-3.5" />
                <span className="hidden sm:inline">Tick 2–6 runs to compare</span>
                <span className="sm:hidden">Tick to compare</span>
              </span>
            }
          >
            <DataTable<Run>
              rows={runs.data}
              loading={runs.isLoading}
              rowKey={(r) => r.id}
              onRowClick={(r) => router.push(`/benchmark/runs/${r.id}`)}
              empty="No run yet"
              columns={[
                {
                  key: "sel",
                  label: "",
                  className: "w-px",
                  render: (r) => (
                    <span onClick={(e) => e.stopPropagation()} className="flex">
                      <Checkbox checked={selected.includes(r.id)} onCheckedChange={(v) => toggle(r.id, v)} label={`Compare run ${r.id}`} />
                    </span>
                  ),
                },
                {
                  key: "target",
                  label: "Target",
                  render: (r) => (
                    <span className="flex min-w-0 flex-col">
                      <Link href={`/benchmark/runs/${r.id}`} onClick={(e) => e.stopPropagation()} className="truncate text-fg hover:underline">
                        {runTarget(r)}
                      </Link>
                      <span className="max-w-56 truncate text-micro text-fg-3">{r.strategy}</span>
                    </span>
                  ),
                },
                { key: "mode", label: "Mode", render: (r) => <Badge tone={r.mode === "live" ? "accent" : "neutral"}>{r.mode}</Badge> },
                {
                  key: "status",
                  label: "Status",
                  render: (r) => (
                    <span className="flex min-w-24 flex-col gap-1">
                      <Badge tone={STATUS_TONE[r.status]} dot>
                        {r.status}
                        {r.status === "running" && r.items_total ? ` · ${r.items_done}/${r.items_total}` : ""}
                      </Badge>
                      {(r.status === "running" || r.status === "queued") && r.items_total > 0 && <ProgressBar value={r.items_done} max={r.items_total} />}
                    </span>
                  ),
                },
                { key: "headline", label: "Measured", render: (r) => <Headline r={r} /> },
                {
                  key: "cost",
                  label: "Cost",
                  className: "text-right",
                  render: (r) => <span className="tabular text-fg-2">{usd(r.cost_usd, r.cost_usd && r.cost_usd < 0.01 ? 4 : 2)}</span>,
                },
                { key: "at", label: "Started", className: "whitespace-nowrap", render: (r) => <span className="text-fg-3">{relTime(r.created_at)}</span> },
              ]}
            />
          </Block>

          {selected.length >= 2 && (
            <Block
              title="Comparison"
              aside={
                <Button size="xs" variant="ghost" onClick={() => setSelected([])}>
                  Clear
                </Button>
              }
            >
              {compare.data ? (
                <>
                  <ComparisonTable
                    columns={compare.data.columns.map((c) => ({
                      key: c.column,
                      label: c.mode === "suite" ? (c.strategy ?? runTarget(c)) : runTarget(c),
                      hint: `${c.mode} · ${c.mode === "suite" ? runTarget(c) : (c.strategy ?? "")}`,
                    }))}
                    rows={compare.data.metrics}
                  />
                  <p className="mt-2 text-meta text-fg-3">{compare.data.note}</p>
                </>
              ) : (
                <Panel className="px-3 py-6 text-center text-meta text-fg-3">{compare.isError ? (compare.error as Error).message : "Loading…"}</Panel>
              )}
            </Block>
          )}
        </>
      )}

      <Dialog open={importOpen} onOpenChange={setImportOpen}>
        {importOpen && <ImportDatasetFlow onDone={() => setImportOpen(false)} />}
      </Dialog>
      <Dialog open={launch !== null} onOpenChange={(v) => !v && setLaunch(null)}>
        {launch !== null && (
          <RunLauncher
            datasets={datasets.data ?? []}
            initialDataset={launch.dataset}
            initialMode={launch.mode}
            onStarted={(r) => {
              setLaunch(null);
              router.push(`/benchmark/runs/${r.id}`);
            }}
          />
        )}
      </Dialog>
    </Page>
  );
}
