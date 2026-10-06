/* Filter helpers shared by the web app and tests. Structural types keep this package dependency-free. */

export interface ConditionLike {
  field: string;
  operator: string;
  value?: unknown;
}

export interface GroupLike {
  op?: "and" | "or";
  conditions?: (ConditionLike | GroupLike)[];
}

export const NO_VALUE_OPERATORS = new Set(["is_empty", "not_empty", "is_true", "is_false", "is_unknown"]);

export function isFilled(v: unknown): boolean {
  if (v === null || v === undefined) return false;
  if (typeof v === "string") return v.trim() !== "";
  if (Array.isArray(v)) return v.length > 0 && v.every((x) => isFilled(x));
  return true;
}

export function isComplete(c: ConditionLike): boolean {
  if (NO_VALUE_OPERATORS.has(c.operator)) return true;
  if (c.operator === "between") return Array.isArray(c.value) && c.value.length === 2 && c.value.every((x) => isFilled(x));
  return isFilled(c.value);
}

/** Drop conditions the user is still typing so the table never flashes empty or errors; null when nothing is left. */
export function completeFilters<G extends GroupLike>(g: G | null | undefined): G | null {
  if (!g?.conditions?.length) return null;
  const conditions = g.conditions.filter((c) => !("field" in c) || isComplete(c));
  return conditions.length ? ({ ...g, op: g.op ?? "and", conditions } as G) : null;
}
