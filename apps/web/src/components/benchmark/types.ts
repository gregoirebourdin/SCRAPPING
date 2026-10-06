/* Benchmark harness — local API types (mirrors scout/services/benchmark.py; packages/schemas is regenerated separately). */

export type MetricUnit = "rate" | "ms" | "usd" | "per_minute" | "count" | "number";

export interface MetricValue {
  label: string;
  group: string;
  unit: MetricUnit | string;
  definition: string;
  value: number | null;
  n: number | null;
  k: number | null;
  ci90: [number, number] | null;
  /** Display order (JSONB does not keep key order). */
  order?: number;
}

export type Metrics = Record<string, MetricValue>;

export interface Dataset {
  id: string;
  name: string;
  kind: "leads" | "email" | "enrichment";
  description: string | null;
  item_count: number;
  summary: { items?: number; people?: number; emails?: number; invalid_emails?: number; enrichment_values?: number };
  people_exhaustive: boolean;
  company_input: "domain" | "name";
  warnings: string[];
  source: string | null;
  runs: number;
  last_run_at: string | null;
  created_at: string;
}

export type RunStatus = "queued" | "running" | "completed" | "failed" | "cancelled";
export type RunMode = "registry" | "live" | "suite";

export interface Run {
  id: string;
  dataset_id: string | null;
  dataset_name: string | null;
  suite_key: string | null;
  mode: RunMode;
  strategy: string | null;
  status: RunStatus;
  items_total: number;
  items_done: number;
  progress: number;
  cost_usd: number;
  duration_ms: number | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  headline: Partial<Record<string, MetricValue>>;
}

export interface RunDetail extends Run {
  config: Record<string, unknown>;
  metrics: Metrics;
  strategies: Record<string, Metrics> | null;
  notes: string[];
}

export interface Suite {
  key: string;
  title: string;
  description: string;
  strategies: string[];
  default_config: Record<string, unknown>;
  demo: boolean;
}

export interface EmailRow {
  person: string | null;
  expected: string | null;
  expected_status: string | null;
  actual: string | null;
  actual_status: string | null;
  verdict: "correct" | "wrong" | "missing" | "invalid_fp" | "invalid_ok";
  company_level?: boolean;
}

export interface Verdicts {
  company?: { tp: number; fp: number; fn: number; given: boolean; expected: string | null; actual: string | null; found: boolean } | null;
  people?: {
    tp: number;
    fp: number;
    fn: number;
    given: boolean;
    exhaustive: boolean;
    pairs: { expected: string; actual: string; role: { expected: string; actual: string | null; correct: boolean | null } | null }[];
    missing: string[];
    spurious: string[];
    unscored: number;
  } | null;
  emails?: { tp: number; fp: number; fn: number; rows: EmailRow[] } | null;
  enrichment?: { rows: { key: string; expected: unknown; actual: unknown; status: string | null; verdict: "correct" | "wrong" | "unknown" }[] } | null;
  catch_all?: { expected: boolean; actual: boolean | null; correct: boolean } | null;
  pattern?: { expected: string; actual: string | null; correct: boolean } | null;
  verdict?: string;
  [key: string]: unknown;
}

export interface ResultRow {
  id: string;
  item_id: string | null;
  ordinal: number;
  label: string | null;
  strategy: string | null;
  expected: Record<string, unknown>;
  actual: Record<string, unknown>;
  verdicts: Verdicts;
  fp: number;
  fn: number;
  latency_ms: number | null;
  cost_usd: number;
  error: string | null;
}

export interface ResultsPage {
  total: number;
  offset: number;
  limit: number;
  items: ResultRow[];
}

export interface Comparison {
  columns: (Run & { column: string; strategy: string | null })[];
  metrics: { key: string; label: string; group: string; unit: string; definition: string; values: (Pick<MetricValue, "value" | "n" | "k" | "ci90"> | null)[] }[];
  note: string;
}

export interface ImportPreview {
  summary: Dataset["summary"];
  mapping: Record<string, string>;
  warnings: string[];
  rows: number;
  sample: { label: string | null; input: Record<string, unknown>; expected: { people?: unknown[]; emails?: unknown[]; enrichment?: Record<string, unknown> } }[];
}

export const GROUP_LABELS: Record<string, string> = {
  company: "Company",
  people: "People",
  email: "Email",
  enrichment: "Enrichment",
  quality: "Errors & duplicates",
  operations: "Time & cost",
  suite: "Suite metrics",
  other: "Other",
};

export const GROUP_ORDER = ["company", "people", "email", "enrichment", "quality", "operations", "suite", "other"];

/** Format a measured value by unit. Rates are 0–1. */
export function fmtMetric(value: number | null | undefined, unit: string): string {
  if (value === null || value === undefined) return "—";
  switch (unit) {
    case "rate":
      return `${(value * 100).toFixed(1)}%`;
    case "ms":
      return value >= 10_000 ? `${(value / 1000).toFixed(1)} s` : `${Math.round(value).toLocaleString("en-US")} ms`;
    case "usd":
      return value === 0 ? "$0" : value < 0.01 ? `$${value.toFixed(4)}` : `$${value.toFixed(2)}`;
    case "per_minute":
      return `${value.toFixed(1)}/min`;
    default:
      return Number.isInteger(value) ? value.toLocaleString("en-US") : value.toFixed(3);
  }
}

export function fmtCi(ci: [number, number] | null | undefined): string | null {
  if (!ci) return null;
  return `${(ci[0] * 100).toFixed(0)}–${(ci[1] * 100).toFixed(0)}%`;
}

export function runTarget(r: Pick<Run, "dataset_name" | "suite_key" | "mode">): string {
  if (r.mode === "suite") return r.suite_key ?? "suite";
  return r.dataset_name ?? "Deleted dataset";
}

export const STATUS_TONE: Record<RunStatus, "neutral" | "accent" | "success" | "danger" | "muted"> = {
  queued: "neutral",
  running: "accent",
  completed: "success",
  failed: "danger",
  cancelled: "muted",
};
