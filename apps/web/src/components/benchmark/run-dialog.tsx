"use client";

import { Badge, Button, Checkbox, cn, DialogContent, Input, Segmented, Spinner, Switch } from "@scout/design-system";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Play } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { api, type ApiError } from "@/lib/api";
import { n } from "@/lib/format";

import type { Dataset, RunDetail, RunMode, Suite } from "./types";

const MODES: { value: RunMode; label: string; hint: string }[] = [
  { value: "registry", label: "Registry", hint: "Compare with what this workspace already holds. Read-only, no cost." },
  { value: "live", label: "Live", hint: "Run the engine on each item now (cache-first crawl, people, emails, columns), under a hard cost cap." },
  { value: "suite", label: "Suite", hint: "Synthetic labelled scenarios; compares strategies side by side. No network, no cost." },
];

function Field({ label, hint, children, className }: { label: string; hint?: string; children: React.ReactNode; className?: string }) {
  return (
    <label className={cn("block min-w-0", className)}>
      <span className="mb-1 block text-meta text-fg-3">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-micro text-fg-3">{hint}</span>}
    </label>
  );
}

const selectClass = "h-7 w-full rounded-sm bg-surface-2 px-1.5 text-body text-fg outline-none shadow-[inset_0_0_0_1px_var(--border-strong)]";

/** Run launcher: mode, target (dataset or suite), strategy options, cost cap. */
export function RunLauncher({
  datasets,
  initialDataset,
  initialMode,
  onStarted,
}: {
  datasets: Dataset[];
  initialDataset?: string;
  initialMode?: RunMode;
  onStarted: (r: RunDetail) => void;
}) {
  const qc = useQueryClient();
  const suites = useQuery({ queryKey: ["benchmark", "suites"], queryFn: () => api<Suite[]>("benchmark/suites"), staleTime: 60_000 });
  const [mode, setMode] = useState<RunMode>(initialMode ?? (datasets.length ? "registry" : "suite"));
  const [datasetId, setDatasetId] = useState(initialDataset ?? datasets[0]?.id ?? "");
  const [suiteKey, setSuiteKey] = useState("");
  const [picked, setPicked] = useState<string[] | null>(null);
  const [label, setLabel] = useState("");
  const [email, setEmail] = useState<"off" | "fast" | "fast_deep">("fast");
  const [peopleAi, setPeopleAi] = useState(false);
  const [enrichAi, setEnrichAi] = useState(false);
  const [maxCost, setMaxCost] = useState("1.00");
  const [maxItems, setMaxItems] = useState("50");
  const [busy, setBusy] = useState(false);

  const suite = (suites.data ?? []).find((s) => s.key === (suiteKey || suites.data?.[0]?.key));
  const strategies = picked ?? suite?.strategies ?? [];
  const dataset = datasets.find((d) => d.id === datasetId);
  const cost = Number(maxCost);
  const items = Number(maxItems);
  const liveInvalid = mode === "live" && (!(cost > 0 && cost <= 25) || !(items >= 1 && items <= 500));
  const canStart = !busy && !liveInvalid && (mode === "suite" ? Boolean(suite) && (!suite?.strategies.length || strategies.length > 0) : Boolean(dataset));

  async function start() {
    setBusy(true);
    try {
      const body =
        mode === "suite"
          ? { mode, suite: suite?.key, strategy: label || undefined, config: suite?.strategies.length ? { strategies } : {} }
          : {
              mode,
              dataset_id: datasetId,
              strategy: label || undefined,
              config: mode === "live" ? { email, people_ai: peopleAi, enrichment_ai: enrichAi, max_cost_usd: cost, max_items: Math.min(items, dataset?.item_count ?? items) } : {},
            };
      const run = await api<RunDetail>("benchmark/runs", { body });
      void qc.invalidateQueries({ queryKey: ["benchmark", "runs"] });
      toast.success("Benchmark run queued");
      onStarted(run);
    } catch (e) {
      const err = e as ApiError;
      toast.error(err.message, err.hint ? { description: err.hint } : undefined);
    } finally {
      setBusy(false);
    }
  }

  return (
    <DialogContent title="New benchmark run" description="Every run reports measured numbers with sample sizes — nothing more." width={620}>
      <div className="max-h-[70vh] space-y-3 overflow-y-auto p-4">
        <div>
          <Segmented value={mode} onChange={setMode} options={MODES.map((m) => ({ value: m.value, label: m.label }))} />
          <p className="mt-1 text-meta text-fg-3">{MODES.find((m) => m.value === mode)?.hint}</p>
        </div>

        {mode !== "suite" ? (
          <Field label="Dataset">
            {datasets.length ? (
              <select value={datasetId} onChange={(e) => setDatasetId(e.target.value)} className={selectClass} aria-label="Dataset">
                {datasets.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name} · {n(d.item_count)} items
                  </option>
                ))}
              </select>
            ) : (
              <p className="text-meta text-fg-3">Import a ground-truth dataset first.</p>
            )}
          </Field>
        ) : (
          <>
            <Field label="Suite">
              {suites.isLoading ? (
                <Spinner />
              ) : suites.data?.length ? (
                <select
                  value={suite?.key ?? ""}
                  onChange={(e) => {
                    setSuiteKey(e.target.value);
                    setPicked(null);
                  }}
                  className={selectClass}
                  aria-label="Suite"
                >
                  {suites.data.map((s) => (
                    <option key={s.key} value={s.key}>
                      {s.title}
                      {s.demo ? " (demo)" : ""}
                    </option>
                  ))}
                </select>
              ) : (
                <p className="text-meta text-fg-3">No suite is registered on this server.</p>
              )}
            </Field>
            {suite && <p className="text-meta text-fg-3">{suite.description}</p>}
            {suite && suite.strategies.length > 0 && (
              <div>
                <span className="mb-1 block text-meta text-fg-3">Strategies compared side by side</span>
                <div className="flex flex-wrap gap-x-4 gap-y-1.5">
                  {suite.strategies.map((s) => (
                    <label key={s} className="flex items-center gap-1.5 text-body text-fg-2">
                      <Checkbox checked={strategies.includes(s)} onCheckedChange={(v) => setPicked(v ? [...strategies, s] : strategies.filter((x) => x !== s))} label={s} />
                      {s.replace(/_/g, " ")}
                    </label>
                  ))}
                </div>
              </div>
            )}
          </>
        )}

        {mode === "live" && dataset && (
          <div className="space-y-3 rounded-md bg-surface-2/50 p-3">
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Email resolution" hint={email === "fast_deep" ? "Waits for SMTP verification (separate jobs)" : undefined}>
                <Segmented
                  value={email}
                  onChange={setEmail}
                  options={[
                    { value: "off", label: "Off" },
                    { value: "fast", label: "Fast" },
                    { value: "fast_deep", label: "Fast + SMTP" },
                  ]}
                />
              </Field>
              <div className="space-y-2">
                <label className="flex items-center justify-between gap-3 text-body text-fg-2">
                  AI people fallback
                  <Switch checked={peopleAi} onCheckedChange={setPeopleAi} label="AI people fallback" />
                </label>
                <label className="flex items-center justify-between gap-3 text-body text-fg-2">
                  AI column strategies
                  <Switch checked={enrichAi} onCheckedChange={setEnrichAi} label="AI column strategies" />
                </label>
              </div>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Cost cap (USD)" hint="Stops launching items once reached (max $25)">
                <Input inputMode="decimal" value={maxCost} onChange={(e) => setMaxCost(e.target.value)} aria-invalid={!(cost > 0 && cost <= 25)} />
              </Field>
              <Field label="Max items" hint={`Dataset has ${n(dataset.item_count)} (max 500)`}>
                <Input inputMode="numeric" value={maxItems} onChange={(e) => setMaxItems(e.target.value)} aria-invalid={!(items >= 1 && items <= 500)} />
              </Field>
            </div>
            <p className="text-micro text-fg-3">
              Engine output (companies, people, emails) is stored in this workspace like a normal run; no list, campaign or exposure is created.
            </p>
          </div>
        )}

        <Field label="Label (optional)" hint="Shown in comparisons; defaults to the options above">
          <Input
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            maxLength={120}
            placeholder={mode === "live" ? `email:${email}` : mode === "suite" ? strategies.join(", ") : "registry snapshot"}
          />
        </Field>
      </div>
      <div className="flex items-center justify-between gap-2 border-t border-line px-4 py-3">
        <span className="min-w-0 truncate text-meta text-fg-3">
          {mode === "live" ? <Badge tone="warning">up to ${Number.isFinite(cost) ? cost.toFixed(2) : "—"}</Badge> : <Badge tone="success">no cost</Badge>}
        </span>
        <Button variant="primary" disabled={!canStart} onClick={() => void start()}>
          {busy ? <Spinner size={12} className="text-accent-contrast" /> : <Play />} Start run
        </Button>
      </div>
    </DialogContent>
  );
}
