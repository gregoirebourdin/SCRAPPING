"use client";

import type { ColumnOut, FieldMeta, FilterGroup, LeadRow, SortSpec } from "@scout/schemas";
import { completeFilters } from "@scout/shared";
import { type InfiniteData, type QueryClient, useInfiniteQuery, useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { qk } from "@/lib/queries";

export interface TableScope {
  key: string; // stable scope key for layout/selection persistence
  kind: "list" | "people" | "companies" | "campaign" | "review";
  listId: string | null;
  campaignId: string | null;
  entityType: "person" | "company";
}

export interface RowsPage {
  rows: LeadRow[];
  next_cursor: string | null;
  total: number;
  columns: ColumnOut[];
}

export const PAGE_SIZE = 400;

export { completeFilters };

/** Lists read like a sheet: oldest first, so leads delivered live are appended at the end and never push the
 * rows the user is looking at. An explicit sort always wins. */
const LIST_ORDER: SortSpec[] = [{ field: "added_at", direction: "asc" }];

export function effectiveSort(scope: TableScope, sort: SortSpec[]): SortSpec[] {
  return sort.length ? sort : scope.kind === "list" ? LIST_ORDER : sort;
}

function fetchPage(scope: TableScope, filters: FilterGroup | null, sort: SortSpec[], search: string, cursor: string | null, signal?: AbortSignal) {
  return api<RowsPage>("rows/query", {
    body: {
      scope: scope.kind,
      list_id: scope.listId,
      campaign_id: scope.campaignId,
      entity_type: scope.entityType,
      filters,
      sort: effectiveSort(scope, sort),
      search: search || null,
      cursor,
      limit: PAGE_SIZE,
      with_total: cursor === null,
    },
    signal,
  });
}

export function rowsKey(scope: TableScope, filters: FilterGroup, sort: SortSpec[], search: string) {
  return [...qk.rows(scope.key), completeFilters(filters), sort, search] as const;
}

export function useRows(scope: TableScope, filters: FilterGroup, sort: SortSpec[], search: string) {
  const activeFilters = completeFilters(filters);
  return useInfiniteQuery({
    queryKey: rowsKey(scope, filters, sort, search),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => fetchPage(scope, activeFilters, sort, search, pageParam, signal),
    getNextPageParam: (last) => last.next_cursor,
    placeholderData: (prev) => prev,
    staleTime: 15_000,
  });
}

/**
 * Live append: re-read only the last loaded page (new rows sort to the end), keep every earlier page as is —
 * no reorder, no scroll jump. When more pages exist beyond what is loaded, only the total moves.
 * Returns the ids that appeared.
 */
export async function refetchTail(qc: QueryClient, scope: TableScope, filters: FilterGroup, sort: SortSpec[], search: string, added: number): Promise<string[]> {
  const key = rowsKey(scope, filters, sort, search);
  const data = qc.getQueryData<InfiniteData<RowsPage, string | null>>(key);
  if (!data || !data.pages.length) {
    await qc.invalidateQueries({ queryKey: key });
    return [];
  }
  const lastIdx = data.pages.length - 1;
  const last = data.pages[lastIdx]!;
  if (last.next_cursor) {
    qc.setQueryData<InfiniteData<RowsPage, string | null>>(key, (d) =>
      d ? { ...d, pages: d.pages.map((p, i) => (i === 0 ? { ...p, total: (p.total ?? 0) + added } : p)) } : d,
    );
    return [];
  }
  const cursor = data.pageParams[lastIdx] ?? null;
  const fresh = await fetchPage(scope, completeFilters(filters), sort, search, cursor);
  const before = new Set(last.rows.map((r) => r.id));
  const appeared = fresh.rows.filter((r) => !before.has(r.id)).map((r) => r.id);
  qc.setQueryData<InfiniteData<RowsPage, string | null>>(key, (d) => {
    if (!d) return d;
    const pages = d.pages.map((p, i) => (i === lastIdx ? { ...fresh, total: i === 0 ? fresh.total : p.total, columns: fresh.columns ?? p.columns } : p));
    if (lastIdx > 0 && pages[0]) pages[0] = { ...pages[0], total: (pages[0].total ?? 0) + appeared.length };
    return { ...d, pages };
  });
  return appeared;
}

export function useFields(entityType: string, listId: string | null) {
  return useQuery({
    queryKey: qk.fields(entityType, listId),
    queryFn: () => api<FieldMeta[]>(`meta/fields?entity_type=${entityType}${listId ? `&list_id=${listId}` : ""}`),
    staleTime: 60_000,
  });
}
