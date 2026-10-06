"use client";

import { Badge, Button, cn } from "@scout/design-system";
import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { Activity as ActivityIcon, Bot, Undo2, User } from "lucide-react";
import Link from "next/link";
import { toast } from "sonner";

import { Page, Panel } from "@/components/common/page";
import { invalidateRows } from "@/components/table/actions";
import { api } from "@/lib/api";
import { n, relTime } from "@/lib/format";
import { qk } from "@/lib/queries";

interface AuditRow {
  id: number;
  actor_type: "user" | "assistant" | "system";
  actor_id: string | null;
  action: string;
  summary: string;
  entity_type: string | null;
  entity_count: number | null;
  campaign_id: string | null;
  can_undo: boolean;
  undone_at: string | null;
  created_at: string;
}

function dayLabel(iso: string): string {
  const d = new Date(iso);
  const today = new Date();
  const y = new Date(Date.now() - 86400000);
  if (d.toDateString() === today.toDateString()) return "Today";
  if (d.toDateString() === y.toDateString()) return "Yesterday";
  return d.toLocaleDateString("en-US", { weekday: "long", month: "short", day: "numeric" });
}

/** Audit log with undo (spec §113–§114). */
export function ActivityPage() {
  const qc = useQueryClient();
  const q = useInfiniteQuery({
    queryKey: qk.activity,
    initialPageParam: null as number | null,
    queryFn: ({ pageParam }) => api<AuditRow[]>(`activity?limit=100${pageParam ? `&before=${pageParam}` : ""}`),
    getNextPageParam: (last) => (last.length === 100 ? last[last.length - 1]!.id : null),
  });
  const rows = q.data?.pages.flat() ?? [];
  const groups: { day: string; rows: AuditRow[] }[] = [];
  for (const r of rows) {
    const day = dayLabel(r.created_at);
    const g = groups[groups.length - 1];
    if (g && g.day === day) g.rows.push(r);
    else groups.push({ day, rows: [r] });
  }

  async function undo(r: AuditRow) {
    try {
      await api(`activity/${r.id}/undo`, { method: "POST" });
      toast("Undone", { description: r.summary });
      void qc.invalidateQueries({ queryKey: qk.activity });
      invalidateRows(qc);
      void qc.invalidateQueries({ queryKey: ["columns"] });
    } catch (e) {
      const err = e as Error & { hint?: string };
      toast.error(err.message, err.hint ? { description: err.hint } : undefined);
    }
  }

  return (
    <Page title="Activity" icon={<ActivityIcon className="size-4 text-fg-3" />} width="narrow">
      {q.isLoading && <p className="text-meta text-fg-3">Loading…</p>}
      {!q.isLoading && rows.length === 0 && (
        <p className="py-12 text-center text-meta text-fg-3">No activity yet. Everything you and the assistant change shows up here, with undo.</p>
      )}
      {groups.map((g) => (
        <section key={g.day} className="mb-5">
          <h2 className="mb-1.5 text-micro font-medium uppercase tracking-wide text-fg-3">{g.day}</h2>
          <Panel className="divide-y divide-line">
            {g.rows.map((r) => (
              <div key={r.id} className={cn("flex items-center gap-3 px-3 py-2", r.undone_at && "opacity-50")}>
                <span
                  className={cn(
                    "grid size-6 shrink-0 place-items-center rounded-full [&_svg]:size-3.5",
                    r.actor_type === "assistant" ? "bg-accent-soft text-accent-strong" : "bg-surface-3 text-fg-3",
                  )}
                >
                  {r.actor_type === "assistant" ? <Bot /> : r.actor_type === "system" ? <ActivityIcon /> : <User />}
                </span>
                <div className="min-w-0 flex-1">
                  <p className={cn("truncate text-body text-fg", r.undone_at && "line-through")}>{r.summary}</p>
                  <p className="text-micro text-fg-3">
                    {r.action.replace(/[._]/g, " ")}
                    {r.entity_count ? ` · ${n(r.entity_count)} ${r.entity_type ?? "item"}${r.entity_count === 1 ? "" : "s"}` : ""}
                    {r.campaign_id && (
                      <>
                        {" · "}
                        <Link href={`/campaigns/${r.campaign_id}`} className="hover:text-fg hover:underline">
                          campaign
                        </Link>
                      </>
                    )}
                  </p>
                </div>
                {r.undone_at && <Badge tone="muted">undone</Badge>}
                {r.can_undo && (
                  <Button size="xs" variant="ghost" onClick={() => void undo(r)}>
                    <Undo2 /> Undo
                  </Button>
                )}
                <span className="w-16 shrink-0 text-right text-micro text-fg-3" title={new Date(r.created_at).toLocaleString()}>
                  {relTime(r.created_at)}
                </span>
              </div>
            ))}
          </Panel>
        </section>
      ))}
      {q.hasNextPage && (
        <div className="flex justify-center">
          <Button variant="ghost" disabled={q.isFetchingNextPage} onClick={() => void q.fetchNextPage()}>
            Load older
          </Button>
        </div>
      )}
    </Page>
  );
}
