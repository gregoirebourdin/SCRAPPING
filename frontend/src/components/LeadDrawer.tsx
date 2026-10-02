"use client";

import { useEffect, useState } from "react";
import { api, type LeadDetail } from "@/lib/api";
import { Badge, Button, Ext, ScoreBar, TierBadge, VerifBadge } from "./ui";

export function LeadDrawer({ id, onClose, onChange }: { id: number; onClose: () => void; onChange: () => void }) {
  const [lead, setLead] = useState<LeadDetail | null>(null);
  const [notes, setNotes] = useState("");

  useEffect(() => {
    api.lead(id).then((l) => {
      setLead(l);
      setNotes(l.notes ?? "");
    });
  }, [id]);

  async function patch(body: { user_status?: string; notes?: string; status?: string }) {
    const l = await api.patchLead(id, body);
    setLead(l);
    onChange();
  }

  return (
    <div className="fixed inset-0 z-20 flex">
      <div className="flex-1 bg-black/20" onClick={onClose} />
      <aside className="h-full w-full max-w-2xl overflow-y-auto border-l border-zinc-200 bg-white p-5 shadow-xl">
        {!lead ? (
          <p className="text-sm text-zinc-500">Loading…</p>
        ) : (
          <div className="space-y-5 text-sm">
            <div className="flex items-start justify-between gap-3">
              <div>
                <h2 className="text-lg font-semibold">{lead.name ?? lead.domain}</h2>
                <div className="flex flex-wrap items-center gap-2 text-zinc-600">
                  <Ext href={lead.website} />
                  {lead.country && <span>· {lead.city ? `${lead.city}, ` : ""}{lead.country}</span>}
                  {lead.founded_year && <span>· since {lead.founded_year}</span>}
                  {lead.team_size_hint && <span>· {lead.team_size_hint}</span>}
                </div>
              </div>
              <Button onClick={onClose}>Close</Button>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <TierBadge tier={lead.tier} />
              <ScoreBar score={lead.score} />
              <Badge tone={lead.status === "qualified" ? "green" : lead.status === "review" ? "amber" : "red"}>{lead.status}</Badge>
              {lead.reject_reason && <Badge tone="red">{lead.reject_reason}</Badge>}
              {lead.last_activity && <Badge>active {lead.last_activity}</Badge>}
              <select value={lead.user_status ?? ""} onChange={(e) => patch({ user_status: e.target.value })} className="rounded border border-zinc-300 px-2 py-1 text-xs">
                <option value="">— pipeline status —</option>
                <option value="new">new</option>
                <option value="contacted">contacted</option>
                <option value="replied">replied</option>
                <option value="meeting">meeting</option>
                <option value="ignored">ignored</option>
              </select>
            </div>

            {lead.tagline && <p className="font-medium">{lead.tagline}</p>}
            {lead.description && <p className="text-zinc-600">{lead.description}</p>}

            <section>
              <h3 className="mb-1 font-semibold">Contact</h3>
              <ul className="space-y-1">
                {lead.emails.length === 0 && <li className="text-zinc-400">no email found</li>}
                {lead.emails.map((e) => (
                  <li key={e.id} className="flex flex-wrap items-center gap-2">
                    <span className={e.is_primary ? "font-medium" : ""}>{e.email}</span>
                    <VerifBadge v={e.verification} />
                    <Badge>{e.source}</Badge>
                    {e.page_url && <Ext href={e.page_url}>source</Ext>}
                  </li>
                ))}
              </ul>
              <div className="mt-2 flex flex-wrap gap-3 text-zinc-700">
                {lead.booking_url && <Ext href={lead.booking_url}>booking page</Ext>}
                {Object.entries(lead.socials ?? {}).map(([k, v]) => (
                  <Ext key={k} href={v}>{k}</Ext>
                ))}
                {lead.phones?.map((p) => <span key={p}>{p}</span>)}
              </div>
            </section>

            <section>
              <h3 className="mb-1 font-semibold">Founder</h3>
              {lead.founder_name || lead.founder_linkedin ? (
                <p>
                  <b>{lead.founder_name ?? "?"}</b> {lead.founder_title && <span className="text-zinc-600">— {lead.founder_title}</span>}{" "}
                  {lead.founder_linkedin && <Ext href={lead.founder_linkedin}>LinkedIn</Ext>}
                </p>
              ) : (
                <p className="text-zinc-400">not identified</p>
              )}
            </section>

            <section>
              <h3 className="mb-1 font-semibold">Positioning</h3>
              <div className="flex flex-wrap gap-1">
                {lead.services.map((s) => <Badge key={s} tone="blue">{s}</Badge>)}
                {lead.icp_signals.map((s) => <Badge key={s} tone="violet">{s}</Badge>)}
              </div>
              <div className="mt-1 flex flex-wrap gap-1">{lead.tech.map((t) => <Badge key={t}>{t}</Badge>)}</div>
            </section>

            <section>
              <h3 className="mb-1 font-semibold">Clients (coaches / infopreneurs) — {lead.clients.length}</h3>
              <ul className="space-y-3">
                {lead.clients.length === 0 && <li className="text-zinc-400">no named client found</li>}
                {lead.clients.map((c) => (
                  <li key={c.id} className="rounded border border-zinc-200 p-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <b>{c.name}</b>
                      <Badge>{c.kind}</Badge>
                      {c.role_title && <Badge tone="violet">{c.role_title}</Badge>}
                      {c.niche && <Badge>{c.niche}</Badge>}
                      <Badge tone={c.status === "resolved" ? "green" : "zinc"}>{c.status}</Badge>
                      <span className="text-xs text-zinc-500">conf {Math.round(c.confidence * 100)}%</span>
                      {c.website && <Ext href={c.website} />}
                    </div>
                    {c.evidence && (
                      <p className="mt-1 text-xs text-zinc-600">
                        “{c.evidence}” {c.evidence_url && <Ext href={c.evidence_url}>↗</Ext>}
                      </p>
                    )}
                    {c.funnels.length > 0 && (
                      <ul className="mt-2 space-y-1 border-l-2 border-emerald-200 pl-2">
                        {c.funnels.map((f) => (
                          <li key={f.id} className="text-xs">
                            <Badge tone="green">{f.funnel_type.replace("_", " ")}</Badge> {f.platform && <Badge>{f.platform}</Badge>}{" "}
                            <Ext href={f.url} /> {f.price_hint && <span className="text-zinc-500">· {f.price_hint}</span>}
                            {f.offer && <div className="text-zinc-700">{f.offer}</div>}
                            {f.steps.length > 0 && <div className="text-zinc-500">{f.steps.map((s) => s.step).join(" → ")}</div>}
                          </li>
                        ))}
                      </ul>
                    )}
                  </li>
                ))}
              </ul>
            </section>

            <section>
              <h3 className="mb-1 font-semibold">Notes</h3>
              <textarea value={notes} onChange={(e) => setNotes(e.target.value)} onBlur={() => notes !== (lead.notes ?? "") && patch({ notes })} rows={3} className="w-full rounded border border-zinc-300 p-2" placeholder="Your notes…" />
            </section>

            <section>
              <h3 className="mb-1 font-semibold">Pages crawled ({lead.pages_crawled})</h3>
              <div className="flex flex-wrap gap-2 text-xs">
                {Object.entries(lead.key_pages ?? {}).map(([k, v]) => (
                  <Ext key={k} href={v}>{k}</Ext>
                ))}
              </div>
              <details className="mt-2 text-xs text-zinc-600">
                <summary className="cursor-pointer">score breakdown</summary>
                <pre className="mt-1 whitespace-pre-wrap rounded bg-zinc-50 p-2">{JSON.stringify(lead.score_breakdown, null, 1)}</pre>
              </details>
            </section>
          </div>
        )}
      </aside>
    </div>
  );
}
