"use client";

import { Button, cn, Input, Segmented } from "@scout/design-system";
import type { FieldMeta, FilterCondition, FilterGroup } from "@scout/schemas";
import { Plus, X } from "lucide-react";

type Op = FilterCondition["operator"];

const OPS: Record<string, { value: Op; label: string }[]> = {
  text: [
    { value: "contains", label: "contains" },
    { value: "not_contains", label: "does not contain" },
    { value: "eq", label: "is" },
    { value: "neq", label: "is not" },
    { value: "starts_with", label: "starts with" },
    { value: "is_empty", label: "is empty" },
    { value: "not_empty", label: "is not empty" },
  ],
  enum: [
    { value: "eq", label: "is" },
    { value: "neq", label: "is not" },
    { value: "in", label: "is any of" },
    { value: "not_in", label: "is none of" },
    { value: "is_empty", label: "is empty" },
    { value: "not_empty", label: "is not empty" },
  ],
  number: [
    { value: "gt", label: ">" },
    { value: "gte", label: "≥" },
    { value: "lt", label: "<" },
    { value: "lte", label: "≤" },
    { value: "eq", label: "=" },
    { value: "between", label: "between" },
    { value: "is_empty", label: "is empty" },
    { value: "not_empty", label: "is not empty" },
  ],
  date: [
    { value: "after", label: "after" },
    { value: "before", label: "before" },
    { value: "between", label: "between" },
    { value: "is_empty", label: "is empty" },
    { value: "not_empty", label: "is not empty" },
  ],
  boolean: [
    { value: "is_true", label: "is true" },
    { value: "is_false", label: "is false" },
    { value: "is_unknown", label: "is unknown" },
  ],
};
OPS.percent = OPS.number!;

const NO_VALUE = new Set<Op>(["is_empty", "not_empty", "is_true", "is_false", "is_unknown"]);

export function opLabel(field: FieldMeta | undefined, op: string): string {
  const list = OPS[field?.type ?? "text"] ?? OPS.text!;
  return list.find((o) => o.value === op)?.label ?? op.replace(/_/g, " ");
}

export function FilterBuilder({ fields, value, onChange }: { fields: FieldMeta[]; value: FilterGroup; onChange: (g: FilterGroup) => void }) {
  const conditions = (value.conditions ?? []).filter((c): c is FilterCondition => "field" in c);
  const byKey = new Map(fields.filter((f) => f.filterable).map((f) => [f.key, f]));
  const filterable = fields.filter((f) => f.filterable);

  const update = (i: number, patch: Partial<FilterCondition>) => {
    const next = conditions.map((c, j) => (j === i ? { ...c, ...patch } : c));
    onChange({ op: value.op ?? "and", conditions: next });
  };
  const remove = (i: number) => onChange({ op: value.op ?? "and", conditions: conditions.filter((_, j) => j !== i) });
  const add = () => {
    const f = filterable[0];
    if (!f) return;
    onChange({ op: value.op ?? "and", conditions: [...conditions, { field: f.key, operator: (OPS[f.type] ?? OPS.text!)[0]!.value, value: null }] });
  };

  return (
    <div className="w-[min(560px,90vw)] space-y-2 p-1">
      <div className="flex items-center justify-between">
        <span className="text-meta text-fg-3">{conditions.length ? "Rows matching" : "No filters"}</span>
        {conditions.length > 1 && (
          <Segmented
            value={value.op ?? "and"}
            onChange={(op) => onChange({ op, conditions })}
            options={[
              { value: "and", label: "All conditions" },
              { value: "or", label: "Any condition" },
            ]}
          />
        )}
      </div>
      {conditions.map((c, i) => {
        const f = byKey.get(c.field);
        const ops = OPS[f?.type ?? "text"] ?? OPS.text!;
        return (
          <div key={i} className="flex items-center gap-1.5">
            <span className="w-10 shrink-0 text-right text-meta text-fg-3">{i === 0 ? "Where" : value.op === "or" ? "or" : "and"}</span>
            <select
              aria-label="Field"
              value={c.field}
              onChange={(e) => {
                const nf = byKey.get(e.target.value);
                update(i, { field: e.target.value, operator: (OPS[nf?.type ?? "text"] ?? OPS.text!)[0]!.value, value: null });
              }}
              className="h-7 w-40 shrink-0 rounded-sm bg-surface-2 px-1.5 text-body text-fg shadow-[inset_0_0_0_1px_var(--border-strong)] outline-none"
            >
              {filterable.map((fm) => (
                <option key={fm.key} value={fm.key}>
                  {fm.label}
                </option>
              ))}
            </select>
            <select
              aria-label="Operator"
              value={c.operator}
              onChange={(e) => update(i, { operator: e.target.value as Op, value: NO_VALUE.has(e.target.value as Op) ? null : c.value })}
              className="h-7 w-32 shrink-0 rounded-sm bg-surface-2 px-1.5 text-body text-fg shadow-[inset_0_0_0_1px_var(--border-strong)] outline-none"
            >
              {ops.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
            <div className="min-w-0 flex-1">{!NO_VALUE.has(c.operator) && <ValueInput field={f} cond={c} onChange={(v) => update(i, { value: v })} />}</div>
            <button type="button" onClick={() => remove(i)} className="rounded-sm p-1 text-fg-3 hover:bg-surface-2 hover:text-fg" aria-label="Remove condition">
              <X className="size-3.5" />
            </button>
          </div>
        );
      })}
      <div className="flex items-center justify-between pt-1">
        <Button size="xs" variant="ghost" onClick={add}>
          <Plus /> Add condition
        </Button>
        {conditions.length > 0 && (
          <Button size="xs" variant="ghost" onClick={() => onChange({ op: "and", conditions: [] })}>
            Clear all
          </Button>
        )}
      </div>
    </div>
  );
}

function ValueInput({ field, cond, onChange }: { field?: FieldMeta; cond: FilterCondition; onChange: (v: unknown) => void }) {
  const cls = "h-7";
  if (field?.type === "enum" && field.enum_values?.length) {
    if (cond.operator === "in" || cond.operator === "not_in") {
      const vals = Array.isArray(cond.value) ? (cond.value as string[]) : [];
      return (
        <div className="flex flex-wrap gap-1">
          {field.enum_values.map((v) => (
            <button
              key={v}
              type="button"
              onClick={() => onChange(vals.includes(v) ? vals.filter((x) => x !== v) : [...vals, v])}
              className={cn(
                "h-6 rounded-xs px-1.5 text-meta shadow-[inset_0_0_0_1px_var(--border-strong)]",
                vals.includes(v) ? "bg-accent-soft text-accent-strong" : "text-fg-2 hover:bg-surface-2",
              )}
            >
              {v.replace(/_/g, " ")}
            </button>
          ))}
        </div>
      );
    }
    return (
      <select
        aria-label="Value"
        value={(cond.value as string) ?? ""}
        onChange={(e) => onChange(e.target.value)}
        className="h-7 w-full rounded-sm bg-surface-2 px-1.5 text-body text-fg shadow-[inset_0_0_0_1px_var(--border-strong)] outline-none"
      >
        <option value="" disabled>
          Choose…
        </option>
        {field.enum_values.map((v) => (
          <option key={v} value={v}>
            {v.replace(/_/g, " ")}
          </option>
        ))}
      </select>
    );
  }
  if (cond.operator === "between") {
    const arr = Array.isArray(cond.value) ? (cond.value as (string | number)[]) : ["", ""];
    const type = field?.type === "date" ? "date" : "number";
    return (
      <div className="flex items-center gap-1">
        <Input className={cls} type={type} value={String(arr[0] ?? "")} onChange={(e) => onChange([e.target.value, arr[1] ?? ""])} aria-label="From" />
        <span className="text-meta text-fg-3">and</span>
        <Input className={cls} type={type} value={String(arr[1] ?? "")} onChange={(e) => onChange([arr[0] ?? "", e.target.value])} aria-label="To" />
      </div>
    );
  }
  if (cond.operator === "in" || cond.operator === "not_in") {
    return (
      <Input
        className={cls}
        placeholder="comma, separated, values"
        value={Array.isArray(cond.value) ? (cond.value as string[]).join(", ") : String(cond.value ?? "")}
        onChange={(e) =>
          onChange(
            e.target.value
              .split(",")
              .map((s) => s.trim())
              .filter(Boolean),
          )
        }
        aria-label="Values"
      />
    );
  }
  const type = field?.type === "number" || field?.type === "percent" ? "number" : field?.type === "date" ? "date" : "text";
  return (
    <Input
      className={cls}
      type={type}
      placeholder={field?.type === "percent" ? "e.g. 80" : "Value"}
      value={cond.value == null ? "" : String(cond.value)}
      onChange={(e) => onChange(e.target.value)}
      aria-label="Value"
    />
  );
}
