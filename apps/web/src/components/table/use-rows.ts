"use client";

import type { ColumnOut, FieldMeta, FilterGroup, LeadRow, SortSpec } from "@scout/schemas";
import { completeFilters } from "@scout/shared";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { qk } from "@/lib/queries";

export interface TableScope {
  key: string; // stable scope key for layout/selection persistence
  kind: "list" | "people" | "companies" | "campaign" | "review";
  listId: string | null;
  campaignId: string | null;
  entityType: "person" | "company";
}

interface Page {
  rows: LeadRow[];
  next_cursor: string | null;
  total: number;
  columns: ColumnOut[];
}

export const PAGE_SIZE = 400;

export { completeFilters };

export function useRows(scope: TableScope, filters: FilterGroup, sort: SortSpec[], search: string) {
  const activeFilters = completeFilters(filters);
  return useInfiniteQuery({
    queryKey: [...qk.rows(scope.key), activeFilters, sort, search],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      api<Page>("rows/query", {
        body: {
          scope: scope.kind,
          list_id: scope.listId,
          campaign_id: scope.campaignId,
          entity_type: scope.entityType,
          filters: activeFilters,
          sort,
          search: search || null,
          cursor: pageParam,
          limit: PAGE_SIZE,
          with_total: pageParam === null,
        },
        signal,
      }),
    getNextPageParam: (last) => last.next_cursor,
    placeholderData: (prev) => prev,
    staleTime: 15_000,
  });
}

export function useFields(entityType: string, listId: string | null) {
  return useQuery({
    queryKey: qk.fields(entityType, listId),
    queryFn: () => api<FieldMeta[]>(`meta/fields?entity_type=${entityType}${listId ? `&list_id=${listId}` : ""}`),
    staleTime: 60_000,
  });
}
