"use client";

import { useState } from "react";
import { api, type Stats } from "@/lib/api";
import { Badge, Button } from "./ui";

export function RunPanel({ stats, refresh }: { stats: Stats | null; refresh: () => void }) {
  const [open, setOpen] = useState(false);
  const [target, setTarget] = useState(1000);
  const [engines, setEngines] = useState<string[]>(["google", "bing", "duckduckgo"]);
  const [maxQueries, setMaxQueries] = useState<number | "">("");
  const [error, setError] = useState<string | null>(null);
  const run = stats?.active_run ?? null;
  const running = run?.status === "running" || run?.status === "pending";
  const s = (run?.stats ?? {}) as Record<string, number | string | Record<string, unknown>>;

  async function start() {
    setError(null);
    try {
      await api.startRun({ target_leads: target, engines, max_queries: maxQueries === "" ? null : maxQueries });
      setOpen(false);
      refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function cancel() {
    if (!run) return;
    await api.cancelRun(run.id);
    refresh();
  }

  const toggle = (e: string) => setEngines((cur) => (cur.includes(e) ? cur.filter((x) => x !== e) : [...cur, e]));

  return (
    <div className="rounded-lg border border-zinc-200 bg-white p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          {run ? (
            <>
              <Badge tone={running ? "blue" : run.status === "completed" ? "green" : run.status === "failed" ? "red" : "zinc"}>
                run #{run.id} · {run.status}
              </Badge>
              <span className="text-zinc-600">stage: <b>{run.stage}</b></span>
              {typeof s.queries_done === "number" && <span className="text-zinc-600">queries {s.queries_done}/{String(s.queries_total)}</span>}
              {typeof s.candidates_total === "number" && <span className="text-zinc-600">candidates {s.candidates_total}</span>}
              {typeof s.crawled === "number" && <span className="text-zinc-600">crawled {s.crawled}</span>}
              {typeof s.agencies_qualified === "number" && <span className="text-zinc-600">qualified {s.agencies_qualified}</span>}
              {typeof s.funnels_found === "number" && <span className="text-zinc-600">funnels {s.funnels_found}</span>}
              {run.error && <span className="text-rose-700">{run.error}</span>}
            </>
          ) : (
            <span className="text-zinc-500">No run yet.</span>
          )}
        </div>
        <div className="flex gap-2">
          {running ? (
            <Button tone="danger" onClick={cancel}>Cancel run</Button>
          ) : (
            <Button tone="primary" onClick={() => setOpen((o) => !o)}>New run</Button>
          )}
        </div>
      </div>
      {stats?.engines && Object.keys(stats.engines).length > 0 && (
        <div className="mt-2 flex flex-wrap gap-2 text-xs text-zinc-600">
          {Object.entries(stats.engines).map(([name, h]) => (
            <span key={name} className="rounded bg-zinc-100 px-2 py-0.5">
              {name}: {String(h.queries)} q · {String(h.results)} res · {h.disabled ? "disabled" : h.available ? "ok" : `cooldown ${String(h.cooldown_remaining)}s`}
              {h.last_error ? ` · ${String(h.last_error)}` : ""}
            </span>
          ))}
        </div>
      )}
      {open && !running && (
        <form
          className="mt-3 flex flex-wrap items-end gap-3 border-t border-zinc-200 pt-3 text-sm"
          onSubmit={(e) => {
            e.preventDefault();
            start();
          }}
        >
          <label className="flex flex-col gap-1">
            <span className="text-xs text-zinc-500">Target leads</span>
            <input type="number" min={1} max={20000} value={target} onChange={(e) => setTarget(Number(e.target.value))} className="w-28 rounded border border-zinc-300 px-2 py-1" />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs text-zinc-500">Max queries (test)</span>
            <input type="number" min={1} value={maxQueries} onChange={(e) => setMaxQueries(e.target.value === "" ? "" : Number(e.target.value))} placeholder="all" className="w-28 rounded border border-zinc-300 px-2 py-1" />
          </label>
          <div className="flex flex-col gap-1">
            <span className="text-xs text-zinc-500">Engines</span>
            <div className="flex gap-2">
              {["google", "bing", "duckduckgo"].map((e) => (
                <label key={e} className="flex items-center gap-1">
                  <input type="checkbox" checked={engines.includes(e)} onChange={() => toggle(e)} /> {e}
                </label>
              ))}
            </div>
          </div>
          <Button tone="primary" type="submit" disabled={engines.length === 0}>Start</Button>
          {error && <span className="text-rose-700">{error}</span>}
        </form>
      )}
    </div>
  );
}
