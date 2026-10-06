"use client";

import type { LeadRow } from "@scout/schemas";
import { type InfiniteData, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import { qk } from "@/lib/queries";

import type { ColumnSpec } from "./cells";
import type { TableScope } from "./use-rows";

export interface EditTarget {
  row: LeadRow;
  spec: ColumnSpec;
}

type Page = { rows: LeadRow[]; next_cursor: string | null; total: number };

function initialValue(t: EditTarget): string {
  if (t.spec.editable === "cell" && t.spec.custom) {
    const c = t.row.cells[t.spec.custom.id];
    if (!c || c.d === "unknown") return "";
    return c.d ?? (c.v == null ? "" : String(c.v));
  }
  return String(t.spec.copyValue?.(t.row) ?? "");
}

/** Inline cell editing. User edits always win over automated values (spec: provenance priority). */
export function useCellEditor(scope: TableScope) {
  const qc = useQueryClient();
  const [target, setTarget] = useState<EditTarget | null>(null);

  const patchRow = useCallback(
    (id: string, patch: (r: LeadRow) => LeadRow) => {
      qc.setQueriesData<InfiniteData<Page>>({ queryKey: qk.rows(scope.key) }, (data) =>
        data ? { ...data, pages: data.pages.map((p) => ({ ...p, rows: p.rows.map((r) => (r.id === id ? patch(r) : r)) })) } : data,
      );
    },
    [qc, scope.key],
  );

  const commit = useCallback(
    async (t: EditTarget, raw: string) => {
      setTarget(null);
      const value = raw.trim();
      if (value === initialValue(t)) return;
      const spec = t.spec;
      const row = t.row;
      const before = row;
      try {
        if (spec.editable === "cell" && spec.custom) {
          const col = spec.custom;
          const parsed: unknown =
            col.data_type === "boolean"
              ? /^(y|yes|true|1|oui)$/i.test(value)
                ? true
                : /^(n|no|false|0|non)$/i.test(value)
                  ? false
                  : value
              : col.data_type === "number"
                ? Number(value)
                : value;
          patchRow(row.id, (r) => ({ ...r, cells: { ...r.cells, [col.id]: { v: parsed, d: typeof parsed === "boolean" ? String(parsed) : value, s: "success", c: 1, u: true } } }));
          await api(`columns/${col.id}/cells`, { method: "PUT", body: { entity_type: scope.entityType, entity_id: row.id, value: value === "" ? null : parsed } });
        } else if (spec.editable === "person" && spec.editField) {
          patchRow(row.id, (r) => ({ ...r, [spec.id === "full_name" ? "full_name" : spec.id]: value }));
          await api(`people/${row.id}`, { method: "PATCH", body: { field: spec.editField, value: value || null } });
        } else if (spec.editable === "company" && spec.editField) {
          const companyId = scope.entityType === "company" ? row.id : row.company_id;
          if (!companyId) return;
          patchRow(row.id, (r) => ({ ...r, company: value }));
          await api(`companies/${companyId}`, { method: "PATCH", body: { field: spec.editField, value: value || null } });
        }
        toast("Saved", { description: `${spec.label} updated — your edit takes priority over automated sources`, duration: 2000 });
        void qc.invalidateQueries({ queryKey: ["person", row.id] });
      } catch (e) {
        patchRow(row.id, () => before);
        const err = e as Error & { hint?: string };
        toast.error(err.message, err.hint ? { description: err.hint } : undefined);
      }
    },
    [patchRow, qc, scope.entityType],
  );

  return { target, begin: setTarget, cancel: () => setTarget(null), commit };
}

export function EditCell({ target, editor }: { target: EditTarget; editor: ReturnType<typeof useCellEditor> }) {
  const ref = useRef<HTMLInputElement>(null);
  const [value, setValue] = useState(() => initialValue(target));
  const done = useRef(false);
  useEffect(() => {
    ref.current?.focus();
    ref.current?.select();
  }, []);
  const finish = (save: boolean) => {
    if (done.current) return;
    done.current = true;
    if (save) void editor.commit(target, value);
    else editor.cancel();
    // hand focus back to the grid for keyboard flow
    requestAnimationFrame(() => (document.querySelector('[role="grid"]') as HTMLElement | null)?.focus());
  };
  return (
    <input
      ref={ref}
      value={value}
      aria-label={`Edit ${target.spec.label}`}
      onChange={(e) => setValue(e.target.value)}
      onClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => {
        e.stopPropagation();
        if (e.key === "Enter") finish(true);
        else if (e.key === "Escape") finish(false);
        else if (e.key === "Tab") {
          e.preventDefault();
          finish(true);
        }
      }}
      onBlur={() => finish(true)}
      className="absolute inset-0 z-10 h-full w-full bg-surface-2 px-2.5 text-table text-fg shadow-[inset_0_0_0_1.5px_var(--accent)] outline-none"
    />
  );
}
