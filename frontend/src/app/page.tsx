"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type Lead, type Stats } from "@/lib/api";
import { LeadDrawer } from "@/components/LeadDrawer";
import { RunPanel } from "@/components/RunPanel";
import { Badge, Button, Ext, ScoreBar, TierBadge, VerifBadge } from "@/components/ui";

type Filters = {
  q: string;
  tier: string;
  status: string;
  min_score: number;
  has_email?: boolean;
  has_client?: boolean;
  has_funnel?: boolean;
  has_linkedin?: boolean;
  country: string;
  sort: string;
  order: string;
};

const DEFAULT: Filters = { q: "", tier: "", status: "qualified,review", min_score: 0, country: "", sort: "score", order: "desc" };

export default function Home() {
  const [filters, setFilters] = useState<Filters>(DEFAULT);
  const [page, setPage] = useState(1);
  const [size] = useState(50);
  const [data, setData] = useState<{ items: Lead[]; total: number } | null>(null);
  const [stats, setStats] = useState<Stats | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .leads({ ...filters, page, size })
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e) => setError((e as Error).message));
  }, [filters, page, size]);

  const loadStats = useCallback(() => {
    api.stats().then(setStats).catch(() => undefined);
  }, []);

  useEffect(load, [load]);
  useEffect(loadStats, [loadStats]);
  useEffect(() => {
    const running = stats?.active_run?.status === "running" || stats?.active_run?.status === "pending";
    const t = setInterval(() => {
      loadStats();
      if (running) load();
    }, running ? 5000 : 20000);
    return () => clearInterval(t);
  }, [stats?.active_run?.status, load, loadStats]);

  const set = (patch: Partial<Filters>) => {
    setFilters((f) => ({ ...f, ...patch }));
    setPage(1);
  };
  const tri = (key: "has_email" | "has_client" | "has_funnel" | "has_linkedin") => {
    const cur = filters[key];
    set({ [key]: cur === undefined ? true : cur === true ? false : undefined });
  };
  const triLabel = (v?: boolean) => (v === undefined ? "any" : v ? "yes" : "no");
  const pages = data ? Math.max(1, Math.ceil(data.total / size)) : 1;

  return (
    <main className="mx-auto max-w-[1600px] space-y-3 p-4">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">LeadForge</h1>
          <p className="text-sm text-zinc-500">Acquisition agencies serving coaches & infopreneurs</p>
        </div>
        {stats && (
          <div className="flex flex-wrap gap-2 text-xs">
            <Badge tone="green">{stats.qualified} qualified</Badge>
            <Badge tone="blue">{stats.with_email} with email</Badge>
            <Badge tone="blue">{stats.with_verified_email} verified</Badge>
            <Badge tone="violet">{stats.with_client} with client</Badge>
            <Badge tone="violet">{stats.with_funnel} with funnel</Badge>
            <Badge>{stats.candidates} candidates · {stats.agencies} crawled · avg {stats.avg_score}</Badge>
          </div>
        )}
        <a href={api.exportUrl({ status: filters.status, tier: filters.tier || undefined, min_score: String(filters.min_score) })} className="rounded-md border border-zinc-900 bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-zinc-700">
          Export CSV
        </a>
      </header>

      <RunPanel stats={stats} refresh={() => { loadStats(); load(); }} />

      <div className="flex flex-wrap items-center gap-2 rounded-lg border border-zinc-200 bg-white p-2 text-sm">
        <input value={filters.q} onChange={(e) => set({ q: e.target.value })} placeholder="Search name, domain, founder…" className="w-64 rounded border border-zinc-300 px-2 py-1" />
        <select value={filters.tier} onChange={(e) => set({ tier: e.target.value })} className="rounded border border-zinc-300 px-2 py-1">
          <option value="">All tiers</option>
          <option value="A">Tier A</option>
          <option value="A,B">Tier A+B</option>
          <option value="B">Tier B</option>
          <option value="C">Tier C</option>
        </select>
        <select value={filters.status} onChange={(e) => set({ status: e.target.value })} className="rounded border border-zinc-300 px-2 py-1">
          <option value="qualified,review">qualified + review</option>
          <option value="qualified">qualified</option>
          <option value="review">review</option>
          <option value="rejected">rejected</option>
        </select>
        <label className="flex items-center gap-1">
          min score
          <input type="number" min={0} max={100} value={filters.min_score} onChange={(e) => set({ min_score: Number(e.target.value) })} className="w-16 rounded border border-zinc-300 px-2 py-1" />
        </label>
        <input value={filters.country} onChange={(e) => set({ country: e.target.value })} placeholder="Country" className="w-32 rounded border border-zinc-300 px-2 py-1" />
        {(["has_email", "has_client", "has_funnel", "has_linkedin"] as const).map((k) => (
          <Button key={k} onClick={() => tri(k)}>
            {k.replace("has_", "")}: {triLabel(filters[k])}
          </Button>
        ))}
        <select value={`${filters.sort}:${filters.order}`} onChange={(e) => { const [sort, order] = e.target.value.split(":"); set({ sort, order }); }} className="rounded border border-zinc-300 px-2 py-1">
          <option value="score:desc">score ↓</option>
          <option value="score:asc">score ↑</option>
          <option value="created:desc">newest</option>
          <option value="name:asc">name</option>
          <option value="country:asc">country</option>
        </select>
        <Button onClick={() => set(DEFAULT)}>Reset</Button>
        <span className="ml-auto text-zinc-500">{data ? `${data.total} leads` : "…"}</span>
      </div>

      {error && <p className="rounded border border-rose-200 bg-rose-50 p-2 text-sm text-rose-700">API error: {error} — is the backend running on {process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"}?</p>}

      <div className="overflow-x-auto rounded-lg border border-zinc-200 bg-white">
        <table className="w-full text-left text-sm">
          <thead className="bg-zinc-50 text-xs uppercase tracking-wide text-zinc-500">
            <tr>
              <th className="px-3 py-2">Tier</th>
              <th className="px-3 py-2">Score</th>
              <th className="px-3 py-2">Agency</th>
              <th className="px-3 py-2">Email</th>
              <th className="px-3 py-2">Founder</th>
              <th className="px-3 py-2">Country</th>
              <th className="px-3 py-2">Services / ICP</th>
              <th className="px-3 py-2">Client (coach)</th>
              <th className="px-3 py-2">Funnel</th>
              <th className="px-3 py-2">Status</th>
            </tr>
          </thead>
          <tbody>
            {data?.items.map((l) => (
              <tr key={l.id} onClick={() => setSelected(l.id)} className="cursor-pointer border-t border-zinc-100 align-top hover:bg-zinc-50">
                <td className="px-3 py-2"><TierBadge tier={l.tier} /></td>
                <td className="px-3 py-2"><ScoreBar score={l.score} /></td>
                <td className="px-3 py-2">
                  <div className="font-medium">{l.name ?? l.domain}</div>
                  <Ext href={l.website} className="text-xs" />
                  {l.tagline && <div className="max-w-xs truncate text-xs text-zinc-500">{l.tagline}</div>}
                </td>
                <td className="px-3 py-2">
                  {l.primary_email ? (
                    <div className="space-y-0.5">
                      <div className="text-xs">{l.primary_email}</div>
                      <VerifBadge v={l.email_verification} />
                      {l.email_count > 1 && <span className="ml-1 text-xs text-zinc-400">+{l.email_count - 1}</span>}
                    </div>
                  ) : (
                    <span className="text-zinc-400">—</span>
                  )}
                </td>
                <td className="px-3 py-2 text-xs">
                  {l.founder_name ?? <span className="text-zinc-400">—</span>}
                  {l.founder_linkedin && <div><Ext href={l.founder_linkedin}>LinkedIn</Ext></div>}
                </td>
                <td className="px-3 py-2 text-xs">{l.country ?? "—"}</td>
                <td className="px-3 py-2">
                  <div className="flex max-w-xs flex-wrap gap-1">
                    {l.services.slice(0, 3).map((s) => <Badge key={s} tone="blue">{s}</Badge>)}
                    {l.icp_signals.slice(0, 3).map((s) => <Badge key={s} tone="violet">{s}</Badge>)}
                  </div>
                </td>
                <td className="px-3 py-2 text-xs">
                  {l.top_client ? (
                    <div>
                      <div className="font-medium">{l.top_client}</div>
                      {l.top_client_role && <div className="text-zinc-500">{l.top_client_role}</div>}
                      {l.top_client_website && <Ext href={l.top_client_website} />}
                      {l.client_count > 1 && <div className="text-zinc-400">+{l.client_count - 1} more</div>}
                    </div>
                  ) : (
                    <span className="text-zinc-400">—</span>
                  )}
                </td>
                <td className="px-3 py-2 text-xs">
                  {l.top_funnel_url ? (
                    <div>
                      <Badge tone="green">{l.top_funnel_type?.replace("_", " ")}</Badge> {l.top_funnel_platform && <Badge>{l.top_funnel_platform}</Badge>}
                      <div className="max-w-[180px] truncate"><Ext href={l.top_funnel_url} /></div>
                    </div>
                  ) : (
                    <span className="text-zinc-400">—</span>
                  )}
                </td>
                <td className="px-3 py-2 text-xs">
                  <Badge tone={l.status === "qualified" ? "green" : l.status === "review" ? "amber" : "red"}>{l.status}</Badge>
                  {l.user_status && <div className="mt-1"><Badge>{l.user_status}</Badge></div>}
                </td>
              </tr>
            ))}
            {data && data.items.length === 0 && (
              <tr><td colSpan={10} className="px-3 py-8 text-center text-zinc-500">No leads match. Start a run or loosen the filters.</td></tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="flex items-center justify-between text-sm text-zinc-600">
        <Button onClick={() => setPage((p) => Math.max(1, p - 1))} disabled={page <= 1}>← Prev</Button>
        <span>page {page} / {pages}</span>
        <Button onClick={() => setPage((p) => Math.min(pages, p + 1))} disabled={page >= pages}>Next →</Button>
      </div>

      {selected !== null && <LeadDrawer id={selected} onClose={() => setSelected(null)} onChange={load} />}
    </main>
  );
}
