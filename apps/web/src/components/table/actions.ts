"use client";

import type { FilterGroup, ListOut, MembershipResult, RowRef } from "@scout/schemas";
import type { QueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { api, download } from "@/lib/api";
import { qk } from "@/lib/queries";
import { useUI } from "@/lib/store";

import { completeFilters, type TableScope } from "./use-rows";

/** Build the row reference for a bulk action: explicit ids, or the whole filtered set ("all matching"). */
export function rowRef(scope: TableScope, ids: string[], opts: { allMatching?: boolean; filters?: FilterGroup | null; search?: string } = {}): RowRef {
  if (opts.allMatching) {
    return {
      entity_type: scope.entityType,
      list_id: scope.listId,
      filter: completeFilters(opts.filters),
      search: opts.search || null,
    };
  }
  return { entity_type: scope.entityType, ids };
}

export function invalidateRows(qc: QueryClient) {
  void qc.invalidateQueries({ queryKey: ["rows"] });
  void qc.invalidateQueries({ queryKey: qk.lists });
  void qc.invalidateQueries({ queryKey: ["list"] });
}

async function undo(qc: QueryClient, auditId: number) {
  try {
    await api(`activity/${auditId}/undo`, { method: "POST" });
    toast("Undone");
    invalidateRows(qc);
  } catch (e) {
    toast.error((e as Error).message);
  }
}

function undoAction(qc: QueryClient, auditId?: number | null) {
  return auditId ? { label: "Undo", onClick: () => void undo(qc, auditId) } : undefined;
}

function fail(e: unknown) {
  const err = e as Error & { hint?: string };
  toast.error(err.message, err.hint ? { description: err.hint } : undefined);
}

function plural(n: number, word: string) {
  return `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;
}

export async function addToList(qc: QueryClient, list: Pick<ListOut, "id" | "name">, ref: RowRef) {
  try {
    const r = await api<MembershipResult>(`lists/${list.id}/members`, { body: { rows: ref } });
    const extra = [r.already_present ? `${r.already_present} already there` : null, r.skipped_suppressed ? `${r.skipped_suppressed} suppressed skipped` : null]
      .filter(Boolean)
      .join(" · ");
    toast.success(`Added ${plural(r.affected, "lead")} to ${list.name}`, { description: extra || undefined, action: undoAction(qc, r.audit_id) });
    invalidateRows(qc);
  } catch (e) {
    fail(e);
  }
}

export async function removeFromList(qc: QueryClient, listId: string, ref: RowRef, clear?: () => void) {
  try {
    const r = await api<MembershipResult>(`lists/${listId}/members/remove`, { body: { rows: ref } });
    toast(`Removed ${plural(r.affected, "lead")} from the list`, { action: undoAction(qc, r.audit_id) });
    clear?.();
    invalidateRows(qc);
  } catch (e) {
    fail(e);
  }
}

export async function moveToList(qc: QueryClient, fromListId: string, to: Pick<ListOut, "id" | "name">, ref: RowRef, clear?: () => void) {
  try {
    const r = await api<MembershipResult>(`lists/${fromListId}/members/move`, { body: { rows: ref, to_list_id: to.id } });
    toast.success(`Moved ${plural(r.affected, "lead")} to ${to.name}`, { action: undoAction(qc, r.audit_id) });
    clear?.();
    invalidateRows(qc);
  } catch (e) {
    fail(e);
  }
}

export async function createListFrom(qc: QueryClient, name: string, ref: RowRef): Promise<ListOut | null> {
  try {
    const l = await api<ListOut>("lists", { body: { name, entity_type: ref.entity_type, from_filter: ref } });
    toast.success(`Created ${l.name}`, { description: l.count ? `${plural(l.count, "lead")}` : undefined });
    invalidateRows(qc);
    return l;
  } catch (e) {
    fail(e);
    return null;
  }
}

export async function exportRows(
  scope: TableScope,
  body: {
    ids?: string[];
    filters?: FilterGroup | null;
    search?: string;
    sort?: unknown[];
    columns?: string[];
    scopeKind: "selected" | "view" | "list";
    viewId?: string | null;
    format?: "csv" | "json";
  },
) {
  const id = toast.loading("Preparing export…");
  try {
    const r = await download("exports", {
      scope: body.scopeKind,
      entity_type: scope.entityType,
      list_id: scope.listId,
      view_id: body.viewId ?? null,
      ids: body.ids ?? null,
      filters: completeFilters(body.filters),
      sort: body.sort ?? [],
      search: body.search || null,
      columns: body.columns ?? null,
      columns_mode: body.columns ? "visible" : "all",
      format: body.format ?? "csv",
    });
    toast.success(`Exported ${plural(r.rows, "row")}`, { id, description: r.filename });
  } catch (e) {
    toast.dismiss(id);
    fail(e);
  }
}

export async function suppressRows(qc: QueryClient, ref: RowRef, reason = "do_not_contact", clear?: () => void) {
  try {
    const r = await api<{ suppressed?: number; audit_id?: number }>("suppression", { body: { rows: ref, reason } });
    toast(`Suppressed ${plural(Number(r.suppressed ?? 0), "contact")}`, { description: "They will never be exported or rediscovered", action: undoAction(qc, r.audit_id) });
    clear?.();
    invalidateRows(qc);
  } catch (e) {
    fail(e);
  }
}

export async function refreshRows(qc: QueryClient, ref: RowRef, what: "company" | "person" | "email" | "column", columnId?: string) {
  try {
    const r = await api<{ queued?: number; skipped_fresh?: number }>("refresh", { body: { rows: ref, what, column_id: columnId ?? null } });
    toast(`Refreshing ${plural(Number(r.queued ?? 0), "lead")}`, { description: r.skipped_fresh ? `${r.skipped_fresh} already fresh — skipped` : undefined });
    invalidateRows(qc);
  } catch (e) {
    fail(e);
  }
}

export async function enrichColumn(
  qc: QueryClient,
  columnId: string,
  columnName: string,
  ref: RowRef | null,
  opts: { force?: boolean; onlyMissing?: boolean; listId?: string | null } = {},
) {
  try {
    const r = await api<{ queued?: number; cached?: number }>(`columns/${columnId}/enrich`, {
      body: { rows: ref, list_id: opts.listId ?? null, force: Boolean(opts.force), only_missing: opts.onlyMissing ?? true },
    });
    toast(`Enriching ${columnName}`, { description: `${plural(Number(r.queued ?? 0), "row")} queued${r.cached ? ` · ${r.cached} reused from cache` : ""}` });
    invalidateRows(qc);
  } catch (e) {
    fail(e);
  }
}

export async function approveRows(qc: QueryClient, entityType: "person" | "company", ids: string[], clear?: () => void) {
  try {
    const r = await api<{ approved?: number }>("review/approve", { body: { entity_type: entityType, ids } });
    toast.success(`Approved ${plural(Number(r.approved ?? ids.length), "lead")}`);
    clear?.();
    invalidateRows(qc);
  } catch (e) {
    fail(e);
  }
}

export async function markContacted(qc: QueryClient, personId: string) {
  try {
    await api(`people/${personId}/contacted`, { method: "POST" });
    toast("Marked as contacted");
    invalidateRows(qc);
  } catch (e) {
    fail(e);
  }
}

export function copyText(text: string, label = "Copied") {
  void navigator.clipboard.writeText(text);
  toast(label, { description: text.length > 80 ? `${text.slice(0, 80)}…` : text, duration: 1500 });
}

export function clearSelection(scopeKey: string) {
  useUI.getState().setSelection(scopeKey, []);
}

/** Company-level columns also apply to people tables (via the person's company); person columns only to people. */
export function columnApplies(c: { entity_type: string }, entityType: "person" | "company") {
  return entityType === "person" || c.entity_type === "company";
}
