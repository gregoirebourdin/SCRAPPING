"use client";

import { Badge, Button, cn, ProgressBar, Segmented, Spinner } from "@scout/design-system";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ChevronDown, ChevronRight, CircleSlash, FlaskConical, Trash2 } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import { Block, Page, Panel } from "@/components/common/page";
import { api, type ApiError } from "@/lib/api";
import { n, relTime, usd } from "@/lib/format";

import { benchmarkKeys } from "./benchmark-page";
import { ComparisonTable, MeasurementNote, MetricGroups, strategyRows } from "./metrics";
import { type EmailRow, fmtMetric, type ResultRow, type ResultsPage, type RunDetail as Run, runTarget, STATUS_TONE } from "./types";

const PAGE = 25;

function duration(ms: number | null): string {
  if (ms === null) return "—";
  if (ms < 1000) return `${ms} ms`;
  const s = ms / 1000;
  return s < 120 ? `${s.toFixed(1)} s` : `${Math.round(s / 60)} min`;
}

/** One benchmark run: measured metrics (value · n · 90 % CI), strategies side by side, per-item diffs. */
export function RunDetailView({ id }: { id: string }) {
  const qc = useQueryClient();
  const router = useRouter();
  const q = useQuery({
    queryKey: benchmarkKeys.run(id),
    queryFn: () => api<Run>(`benchmark/runs/${id}`),
    refetchInterval: (query) => (query.state.data && ["queued", "running"].includes(query.state.data.status) ? 1_500 : false),
    retry: (c, e) => ![403, 404].includes((e as ApiError).status) && c < 2,
  });
  const r = q.data;
  const active = r?.status === "queued" || r?.status === "running";

  async function cancel() {
    try {
      await api(`benchmark/runs/${id}/cancel`, { method: "POST" });
      void qc.invalidateQueries({ queryKey: ["benchmark"] });
    } catch (e) {
      toast.error((e as Error).message);
    }
  }

  async function remove() {
    if (!window.confirm("Delete this run and its per-item results?")) return;
    try {
      await api(`benchmark/runs/${id}`, { method: "DELETE" });
      void qc.invalidateQueries({ queryKey: benchmarkKeys.runs });
      router.push("/benchmark");
    } catch (e) {
      toast.error((e as Error).message);
    }
  }

  return (
    <Page
      title={r ? `${runTarget(r)} · ${r.mode}` : "Benchmark run"}
      icon={<FlaskConical className="size-4 text-fg-3" />}
      actions={
        <>
          <Link href="/benchmark" className="inline-flex h-7 items-center gap-1.5 rounded-sm px-2 text-body text-fg-2 hover:bg-surface-2 hover:text-fg">
            <ArrowLeft className="size-3.5" /> <span className="hidden sm:inline">Benchmark</span>
          </Link>
          {active ? (
            <Button variant="ghost" onClick={() => void cancel()}>
              <CircleSlash /> <span className="hidden sm:inline">Cancel</span>
            </Button>
          ) : (
            r && (
              <Button variant="ghost" onClick={() => void remove()} aria-label="Delete run">
                <Trash2 />
              </Button>
            )
          )}
        </>
      }
    >
      {q.isError ? (
        <Panel className="px-4 py-6 text-center text-meta text-fg-3">
          {(q.error as ApiError).status === 403 ? "The benchmark harness is restricted to workspace admins." : (q.error as Error).message}
        </Panel>
      ) : !r ? (
        <div className="flex justify-center py-10">
          <Spinner size={16} />
        </div>
      ) : (
        <>
          <div className="mb-4 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-meta text-fg-3">
            <Badge tone={STATUS_TONE[r.status]} dot>
              {r.status}
            </Badge>
            {r.strategy && <span className="min-w-0 max-w-full truncate text-fg-2">{r.strategy}</span>}
            <span className="tabular">
              {n(r.items_done)}/{n(r.items_total)} items
            </span>
            <span className="tabular">{duration(r.duration_ms)}</span>
            <span className="tabular">{usd(r.cost_usd, r.cost_usd && r.cost_usd < 0.01 ? 4 : 2)}</span>
            <span>{relTime(r.created_at)}</span>
          </div>
          {active && r.items_total > 0 && <ProgressBar value={r.items_done} max={r.items_total} className="mb-4" />}
          {r.error && <p className="mb-4 break-words rounded-md bg-danger-soft px-3 py-2 text-meta text-danger">{r.error}</p>}
          {r.notes.length > 0 && (
            <ul className="mb-4 space-y-0.5 text-meta text-warning">
              {r.notes.map((note) => (
                <li key={note}>{note}</li>
              ))}
            </ul>
          )}
          <MeasurementNote className="mb-5 max-w-3xl" suite={r.mode === "suite"} />

          {active && !Object.keys(r.metrics).length ? (
            <Panel className="mb-6 flex items-center gap-2 px-4 py-6 text-meta text-fg-3">
              <Spinner /> Metrics are computed when the run completes.
            </Panel>
          ) : (
            <MetricGroups metrics={r.metrics} />
          )}

          {r.strategies && Object.keys(r.strategies).length > 0 && (
            <Block title="Strategies side by side" aside={<span className="hidden text-meta text-fg-3 sm:inline">Same scenarios for every strategy</span>}>
              <ComparisonTable {...strategyRows(r.strategies)} />
            </Block>
          )}

          <Results run={r} />
        </>
      )}
    </Page>
  );
}

function Results({ run }: { run: Run }) {
  const [offset, setOffset] = useState(0);
  const [filter, setFilter] = useState<"all" | "errors">("errors");
  const strategies = Object.keys(run.strategies ?? {});
  const [strategy, setStrategy] = useState<string>("");
  const qs = `offset=${offset}&limit=${PAGE}${filter === "errors" ? "&errors_only=true" : ""}${strategy ? `&strategy=${encodeURIComponent(strategy)}` : ""}`;
  const q = useQuery({
    queryKey: ["benchmark", "results", run.id, qs, run.items_done],
    queryFn: () => api<ResultsPage>(`benchmark/runs/${run.id}/results?${qs}`),
    placeholderData: (prev) => prev,
  });
  const page = q.data;

  return (
    <Block
      title={`Per-item diffs${page ? ` · ${n(page.total)}` : ""}`}
      aside={
        <span className="flex flex-wrap items-center justify-end gap-1.5">
          {strategies.length > 0 && (
            <select
              value={strategy}
              onChange={(e) => {
                setStrategy(e.target.value);
                setOffset(0);
              }}
              aria-label="Strategy"
              className="h-7 max-w-36 rounded-sm bg-surface-2 px-1.5 text-meta text-fg outline-none shadow-[inset_0_0_0_1px_var(--border-strong)]"
            >
              <option value="">All strategies</option>
              {strategies.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          )}
          <Segmented
            value={filter}
            onChange={(v) => {
              setFilter(v);
              setOffset(0);
            }}
            options={[
              { value: "errors", label: "FP / FN" },
              { value: "all", label: "All" },
            ]}
          />
        </span>
      }
    >
      <Panel className="divide-y divide-line">
        {q.isLoading && (
          <div className="flex justify-center py-6">
            <Spinner />
          </div>
        )}
        {page && !page.items.length && (
          <p className="px-3 py-6 text-center text-meta text-fg-3">{filter === "errors" ? "No false positive or false negative." : "No result yet."}</p>
        )}
        {page?.items.map((row) => (
          <ResultItem key={row.id} row={row} />
        ))}
      </Panel>
      {page && page.total > PAGE && (
        <div className="mt-2 flex items-center justify-end gap-2 text-meta text-fg-3">
          <span className="tabular">
            {offset + 1}–{Math.min(offset + PAGE, page.total)} of {n(page.total)}
          </span>
          <Button size="xs" variant="secondary" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
            Previous
          </Button>
          <Button size="xs" variant="secondary" disabled={offset + PAGE >= page.total} onClick={() => setOffset(offset + PAGE)}>
            Next
          </Button>
        </div>
      )}
    </Block>
  );
}

function ResultItem({ row }: { row: ResultRow }) {
  const [open, setOpen] = useState(false);
  const v = row.verdicts;
  return (
    <div>
      <button type="button" onClick={() => setOpen(!open)} className="flex w-full min-w-0 items-center gap-2 px-3 py-2 text-left hover:bg-row-hover" aria-expanded={open}>
        {open ? <ChevronDown className="size-3.5 shrink-0 text-fg-3" /> : <ChevronRight className="size-3.5 shrink-0 text-fg-3" />}
        <span className="tabular w-8 shrink-0 text-micro text-fg-3">#{row.ordinal}</span>
        <span className="min-w-0 flex-1 truncate text-body text-fg">{row.label ?? "—"}</span>
        {row.strategy && (
          <Badge tone="neutral" className="hidden sm:inline-flex">
            {row.strategy}
          </Badge>
        )}
        {row.error && <Badge tone="danger">error</Badge>}
        {row.fp > 0 && <Badge tone="warning">FP {row.fp}</Badge>}
        {row.fn > 0 && <Badge tone="danger">FN {row.fn}</Badge>}
        {!row.fp && !row.fn && !row.error && <Badge tone="success">match</Badge>}
        <span className="tabular hidden w-16 shrink-0 text-right text-micro text-fg-3 sm:inline">{row.latency_ms != null ? `${row.latency_ms} ms` : ""}</span>
      </button>
      {open && (
        <div className="space-y-3 px-3 pb-3 pl-6 text-meta sm:pl-12">
          {row.error && <p className="break-words text-danger">{row.error}</p>}
          {v.company || v.people || v.emails || v.enrichment ? <DatasetDiff v={v} /> : <SuiteDiff row={row} />}
        </div>
      )}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="min-w-0">
      <h4 className="mb-1 text-micro font-medium uppercase tracking-wide text-fg-3">{title}</h4>
      {children}
    </section>
  );
}

function Pair({ expected, actual, tone }: { expected: React.ReactNode; actual: React.ReactNode; tone: "ok" | "fp" | "fn" | "wrong" | "muted" }) {
  return (
    <div className="grid grid-cols-[1fr_1fr] gap-2 border-b border-line/40 py-1 last:border-0">
      <span className={cn("min-w-0 break-words", tone === "fn" || tone === "wrong" ? "text-danger" : "text-fg-2")}>{expected}</span>
      <span className={cn("min-w-0 break-words", tone === "ok" ? "text-success" : tone === "fp" || tone === "wrong" ? "text-warning" : "text-fg-3")}>{actual}</span>
    </div>
  );
}

const EMAIL_TONE: Record<EmailRow["verdict"], "ok" | "fp" | "fn" | "wrong" | "muted"> = {
  correct: "ok",
  wrong: "wrong",
  missing: "fn",
  invalid_fp: "fp",
  invalid_ok: "muted",
};

function show(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function DatasetDiff({ v }: { v: ResultRow["verdicts"] }) {
  return (
    <>
      <div className="grid grid-cols-[1fr_1fr] gap-2 text-micro uppercase tracking-wide text-fg-3">
        <span>Expected</span>
        <span>Engine</span>
      </div>
      {v.company && (
        <Section title={`Company${v.company.given ? " · identity given as input" : ""}`}>
          <Pair
            expected={v.company.expected}
            actual={v.company.found ? (v.company.actual ?? "found, no domain") : "not found"}
            tone={v.company.tp ? "ok" : v.company.fp ? "wrong" : "fn"}
          />
        </Section>
      )}
      {v.people && (
        <Section title={`People · TP ${v.people.tp} · FP ${v.people.fp} · FN ${v.people.fn}${v.people.given ? " · names given" : ""}`}>
          {v.people.pairs.map((p) => (
            <Pair
              key={p.expected}
              expected={
                <>
                  {p.expected}
                  {p.role && <span className="text-fg-3"> · {p.role.expected}</span>}
                </>
              }
              actual={
                <>
                  {p.actual}
                  {p.role && <span className={cn(p.role.correct === false ? "text-warning" : "text-fg-3")}> · {p.role.actual ?? "no title"}</span>}
                </>
              }
              tone="ok"
            />
          ))}
          {v.people.missing.map((m) => (
            <Pair key={`m-${m}`} expected={m} actual="missing" tone="fn" />
          ))}
          {v.people.spurious.map((s) => (
            <Pair key={`s-${s}`} expected="—" actual={`${s} (not in truth)`} tone="fp" />
          ))}
          {v.people.unscored > 0 && <p className="text-fg-3">{v.people.unscored} other people found (truth is partial: not scored)</p>}
        </Section>
      )}
      {v.emails && v.emails.rows.length > 0 && (
        <Section title={`Emails · TP ${v.emails.tp} · FP ${v.emails.fp} · FN ${v.emails.fn}`}>
          {v.emails.rows.map((e, i) => (
            <Pair
              key={i}
              expected={
                <>
                  {e.person && <span className="text-fg-3">{e.person}: </span>}
                  {e.expected ?? "no mailbox"}
                  {e.expected_status && <span className="text-fg-3"> · {e.expected_status}</span>}
                </>
              }
              actual={
                <>
                  {e.actual ?? "—"}
                  {e.actual_status && <span className="text-fg-3"> · {e.actual_status}</span>}
                  <span className="text-fg-3"> · {e.verdict.replace("_", " ")}</span>
                </>
              }
              tone={EMAIL_TONE[e.verdict]}
            />
          ))}
        </Section>
      )}
      {(v.catch_all || v.pattern) && (
        <Section title="Domain">
          {v.catch_all && (
            <Pair
              expected={`catch-all: ${v.catch_all.expected ? "yes" : "no"}`}
              actual={`catch-all: ${v.catch_all.actual === null ? "unknown" : v.catch_all.actual ? "yes" : "no"}`}
              tone={v.catch_all.correct ? "ok" : "wrong"}
            />
          )}
          {v.pattern && <Pair expected={`pattern ${v.pattern.expected}`} actual={`pattern ${v.pattern.actual ?? "unknown"}`} tone={v.pattern.correct ? "ok" : "wrong"} />}
        </Section>
      )}
      {v.enrichment && v.enrichment.rows.length > 0 && (
        <Section title="Columns">
          {v.enrichment.rows.map((c) => (
            <Pair
              key={c.key}
              expected={
                <>
                  <span className="text-fg-3">{c.key}: </span>
                  {show(c.expected)}
                </>
              }
              actual={c.verdict === "unknown" ? (c.status ?? "unknown") : show(c.actual)}
              tone={c.verdict === "correct" ? "ok" : c.verdict === "wrong" ? "wrong" : "fn"}
            />
          ))}
        </Section>
      )}
    </>
  );
}

function SuiteDiff({ row }: { row: ResultRow }) {
  const verdict = typeof row.verdicts.verdict === "string" ? row.verdicts.verdict : null;
  return (
    <>
      {verdict && (
        <p>
          Verdict: <span className={cn(verdict === "correct" ? "text-success" : "text-warning")}>{verdict.replace(/_/g, " ")}</span>
          {row.cost_usd ? <span className="text-fg-3"> · {fmtMetric(row.cost_usd, "usd")}</span> : null}
        </p>
      )}
      <div className="grid gap-2 sm:grid-cols-2">
        <Section title="Expected">
          <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded-sm bg-surface-2 p-2 font-mono text-[11px] text-fg-2">{JSON.stringify(row.expected, null, 1)}</pre>
        </Section>
        <Section title="Actual">
          <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded-sm bg-surface-2 p-2 font-mono text-[11px] text-fg-2">{JSON.stringify(row.actual, null, 1)}</pre>
        </Section>
      </div>
    </>
  );
}
