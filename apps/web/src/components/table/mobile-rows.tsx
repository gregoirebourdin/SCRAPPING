"use client";

import { EmailStatusBadge, Skeleton } from "@scout/design-system";
import type { LeadRow } from "@scout/schemas";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useEffect, useRef } from "react";

import { useUI } from "@/lib/store";

import { Avatar, ScoreCell } from "./cells";

const ROW = 64;

/** Phone layout (spec §163): read-only lead cards, tap to open the drawer. No spreadsheet UX on small screens. */
export function MobileRows({
  rows,
  entityType,
  loading,
  hasMore,
  fetchingMore,
  onLoadMore,
  emptyState,
}: {
  rows: LeadRow[];
  entityType: "person" | "company";
  loading: boolean;
  hasMore: boolean;
  fetchingMore: boolean;
  onLoadMore: () => void;
  emptyState?: React.ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const openDrawer = useUI((s) => s.openDrawer);
  const count = rows.length + (loading && !rows.length ? 10 : hasMore ? 3 : 0);
  // eslint-disable-next-line react-hooks/incompatible-library
  const v = useVirtualizer({ count, getScrollElement: () => ref.current, estimateSize: () => ROW, overscan: 8 });
  const items = v.getVirtualItems();
  const last = items[items.length - 1]?.index ?? 0;
  useEffect(() => {
    if (hasMore && !fetchingMore && rows.length && last >= rows.length - 20) onLoadMore();
  }, [last, hasMore, fetchingMore, rows.length, onLoadMore]);

  if (!loading && !rows.length) return <div className="grid flex-1 place-items-center p-6">{emptyState}</div>;

  return (
    <div ref={ref} className="min-h-0 flex-1 overflow-y-auto">
      <div style={{ height: v.getTotalSize(), position: "relative" }}>
        {items.map((it) => {
          const r = rows[it.index];
          return (
            <div key={r?.id ?? `ph-${it.index}`} className="absolute inset-x-0 border-b border-line/60" style={{ height: ROW, transform: `translateY(${it.start}px)` }}>
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
