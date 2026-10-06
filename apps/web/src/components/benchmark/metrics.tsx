"use client";

import { cn, Tip } from "@scout/design-system";

import { Block, Panel } from "@/components/common/page";

import { fmtCi, fmtMetric, GROUP_LABELS, GROUP_ORDER, type Metrics, type MetricValue } from "./types";

/** One measured number: value, sample size and Wilson 90 % interval — never a bare percentage. */
export function MetricCard({ m }: { m: MetricValue }) {
  const ci = fmtCi(m.ci90);
  const empty = m.value === null || m.value === undefined;
  return (
    <Tip content={<span className="block max-w-72 whitespace-normal">{m.definition || m.label}</span>}>
      <div className="min-w-0 rounded-md bg-surface-1 px-3 py-2.5 shadow-[inset_0_0_0_1px_var(--border-subtle)]">
        <div className="truncate text-meta text-fg-3">{m.label}</div>
        <div className={cn("tabular mt-0.5 text-title", empty ? "text-fg-3" : "text-fg")}>{fmtMetric(m.value, m.unit)}</div>
        <div className="mt-0.5 truncate text-micro text-fg-3">
          {m.unit === "rate" ? (
            m.n ? (
              <>
                {m.k ?? "?"}/{m.n}
                {ci && <span className="text-fg-3"> · 90% CI {ci}</span>}
              </>
            ) : (
              "no sample"
            )
          ) : m.n !== null && m.n !== undefined ? (
            `n = ${m.n.toLocaleString("en-US")}`
          ) : (
            " "
          )}
        </div>
      </div>
    </Tip>
  );
}

export function groupMetrics(metrics: Metrics): { group: string; items: [string, MetricValue][] }[] {
  const by: Record<string, [string, MetricValue][]> = {};
  for (const [k, m] of Object.entries(metrics)) (by[m.group] ??= []).push([k, m]);
  const groups = Object.keys(by).sort((a, b) => (GROUP_ORDER.indexOf(a) + 1 || 99) - (GROUP_ORDER.indexOf(b) + 1 || 99));
  return groups.map((g) => ({ group: g, items: by[g] ?? [] }));
}

export function MetricGroups({ metrics }: { metrics: Metrics }) {
  const groups = groupMetrics(metrics);
  if (!groups.length) return <p className="text-meta text-fg-3">No metric yet.</p>;
  return (
    <>
      {groups.map(({ group, items }) => (
        <Block key={group} title={GROUP_LABELS[group] ?? group}>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
            {items.map(([k, m]) => (
              <MetricCard key={k} m={m} />
            ))}
          </div>
        </Block>
      ))}
    </>
  );
}

type Cell = Pick<MetricValue, "value" | "n" | "k" | "ci90"> | null | undefined;

/** Metrics side by side (strategies of a suite run, or several runs). Shows n and the 90 % interval; no winner is declared. */
export function ComparisonTable({
  columns,
  rows,
}: {
  columns: { key: string; label: React.ReactNode; hint?: React.ReactNode }[];
  rows: { key: string; label: string; unit: string; definition?: string; values: Cell[] }[];
}) {
  return (
    <Panel className="overflow-x-auto">
      <table className="w-full min-w-[480px] text-table">
        <thead>
          <tr className="border-b border-line text-left text-meta text-fg-3">
            <th className="sticky left-0 z-[1] h-8 bg-surface-1 px-3 font-medium">Metric</th>
            {columns.map((c) => (
              <th key={c.key} className="h-8 px-3 text-right align-bottom font-medium">
                <span className="block text-fg-2">{c.label}</span>
                {c.hint && <span className="block text-micro font-normal text-fg-3">{c.hint}</span>}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key} className="border-b border-line/60 last:border-0">
              <td className="sticky left-0 z-[1] bg-surface-1 px-3 py-1.5 text-fg" title={r.definition}>
                {r.label}
              </td>
              {r.values.map((v, i) => {
                const ci = fmtCi(v?.ci90);
                return (
                  <td key={i} className="px-3 py-1.5 text-right align-top">
                    <span className="tabular block text-fg">{fmtMetric(v?.value, r.unit)}</span>
                    {v && (r.unit === "rate" ? v.n : v.n != null) ? (
                      <span className="tabular block whitespace-nowrap text-micro text-fg-3">
                        {r.unit === "rate" ? `${v.k ?? "?"}/${v.n}` : `n=${v.n}`}
                        {ci ? ` · ${ci}` : ""}
                      </span>
                    ) : null}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </Panel>
  );
}

/** Rows for a run's strategies (suite runs) in metric-definition order. */
export function strategyRows(strategies: Record<string, Metrics>) {
  const names = Object.keys(strategies);
  const keys: string[] = [];
  for (const n of names) for (const k of Object.keys(strategies[n] ?? {})) if (!keys.includes(k)) keys.push(k);
  return {
    columns: names.map((n) => ({ key: n, label: n.replace(/_/g, " ") })),
    rows: keys.map((k) => {
      const sample = names.map((n) => strategies[n]?.[k]).find(Boolean);
      return { key: k, label: sample?.label ?? k, unit: sample?.unit ?? "number", definition: sample?.definition, values: names.map((n) => strategies[n]?.[k] ?? null) };
    }),
  };
}

export function MeasurementNote({ className }: { className?: string }) {
  return (
    <p className={cn("text-meta text-fg-3", className)}>
      Measured on your own ground truth only — each rate shows its sample size and a 90% confidence interval. Research makes no comparative claim (for example against Apollo)
      without a head-to-head measurement on the same dataset; overlapping intervals are not a difference.
    </p>
  );
}
