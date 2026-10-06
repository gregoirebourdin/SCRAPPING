"use client";

import { cn, EmailStatusBadge, ShimmerText, Skeleton } from "@scout/design-system";
import type { LeadRow } from "@scout/schemas";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useEffect, useRef } from "react";

import { useUI } from "@/lib/store";

import { Avatar, ScoreCell } from "./cells";
import type { PendingRow } from "./lead-table";

const ROW = 64;

/** Phone layout (spec §163): read-only lead cards, tap to open the drawer. No spreadsheet UX on small screens.
 * While a search fills the list, in-flight companies show as shimmering cards at the end and new leads slide in. */
export function MobileRows({
  rows,
  entityType,
  loading,
  hasMore,
  fetchingMore,
  onLoadMore,
  emptyState,
  pending = [],
  pendingMore = 0,
  fresh,
}: {
  rows: LeadRow[];
  entityType: "person" | "company";
  loading: boolean;
  hasMore: boolean;
  fetchingMore: boolean;
  onLoadMore: () => void;
  emptyState?: React.ReactNode;
  pending?: PendingRow[];
  pendingMore?: number;
  fresh?: Record<string, number>;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const openDrawer = useUI((s) => s.openDrawer);
  const live = !loading && !hasMore ? pending.length + (pendingMore > 0 ? 1 : 0) : 0;
  const count = rows.length + live + (loading && !rows.length ? 10 : hasMore ? 3 : 0);
  // eslint-disable-next-line react-hooks/incompatible-library
  const v = useVirtualizer({ count, getScrollElement: () => ref.current, estimateSize: () => ROW, overscan: 8 });
  const items = v.getVirtualItems();
  const last = items[items.length - 1]?.index ?? 0;
  useEffect(() => {
    if (hasMore && !fetchingMore && rows.length && last >= rows.length - 20) onLoadMore();
  }, [last, hasMore, fetchingMore, rows.length, onLoadMore]);

  if (!loading && !rows.length && !live) return <div className="grid flex-1 place-items-center p-6">{emptyState}</div>;

  return (
    <div ref={ref} className="min-h-0 flex-1 overflow-y-auto">
      <div style={{ height: v.getTotalSize(), position: "relative" }}>
        {items.map((it) => {
          const r = rows[it.index];
          const li = !r && it.index - rows.length < live ? it.index - rows.length : -1;
          const p = li >= 0 ? pending[li] : undefined;
          return (
            <div
              key={r?.id ?? (p ? `live-${p.key}` : li >= 0 ? "live-more" : `ph-${it.index}`)}
              className={cn("absolute inset-x-0 border-b border-line/60", r && fresh?.[r.id] && "row-enter")}
              style={{ height: ROW, transform: `translateY(${it.start}px)` }}
            >
              {r ? (
                <button type="button" onClick={() => openDrawer(entityType, r.id)} className="flex h-full w-full items-center gap-3 px-4 text-left active:bg-surface-2">
                  <Avatar name={entityType === "person" ? r.full_name : r.company} size={32} />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-body font-medium text-fg">{entityType === "person" ? r.full_name : r.company}</span>
                    <span className="block truncate text-meta text-fg-3">
                      {entityType === "person" ? [r.title, r.company].filter(Boolean).join(" · ") : [r.industry, r.city].filter(Boolean).join(" · ")}
                    </span>
                  </span>
                  <span className="flex shrink-0 flex-col items-end gap-1">
                    {entityType === "person" ? <EmailStatusBadge status={r.email_status} /> : null}
                    {r.icp_score != null && <ScoreCell value={r.icp_score} />}
                  </span>
                </button>
              ) : li >= 0 ? (
                <div className="flex h-full items-center gap-3 px-4 animate-fade-in" aria-busy="true">
                  <span className="grid size-8 shrink-0 place-items-center rounded-full bg-surface-2">
                    <span className={cn("size-1.5 rounded-full", p?.paused ? "bg-warning/70" : "bg-accent animate-pulse-soft")} />
                  </span>
                  {p ? (
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-body text-fg-2">{p.title}</span>
                      <span className="block truncate text-meta">{p.paused ? <span className="text-fg-3">{p.stage}</span> : <ShimmerText>{p.stage}…</ShimmerText>}</span>
                    </span>
                  ) : (
                    <span className="text-meta text-fg-3">+{pendingMore.toLocaleString()} more companies queued</span>
                  )}
                </div>
              ) : (
                <div className="flex h-full items-center gap-3 px-4">
                  <Skeleton className="size-8 rounded-full" />
                  <div className="flex-1 space-y-2">
                    <Skeleton className="h-3 w-1/2" />
                    <Skeleton className="h-2.5 w-3/4" />
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
