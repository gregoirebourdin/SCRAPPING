/* Shared contracts generated from the FastAPI OpenAPI document (`pnpm schemas:generate`).
   Do not hand-edit api.d.ts; add convenience aliases here. */
import type { components, paths } from "./api";

export type { components, paths };
type S = components["schemas"];

export type MeOut = S["MeOut"];
export type WorkspaceOut = S["WorkspaceOut"];
export type ListOut = S["ListOut"];
export type RowsOut = S["RowsOut"];
export type RowsQuery = S["RowsQuery"];
export type ColumnOut = S["ColumnOut"];
export type ViewOut = S["ViewOut"];
export type FieldMeta = S["FieldMeta"];
export type FilterGroup = S["FilterGroup"];
export type FilterCondition = S["FilterCondition"];
export type SortSpec = S["SortSpec"];
export type CampaignDefinition = S["CampaignDefinition"];
export type ParseOut = S["ParseOut"];
export type MembershipResult = S["MembershipResult"];
export type RowRef = S["RowRef"];
export type EntityType = "person" | "company";
export type EmailStatus = "SAFE" | "RISKY" | "CATCH_ALL" | "UNKNOWN" | "INVALID";
export type CellStatus = "not_started" | "queued" | "running" | "success" | "unknown" | "failed" | "stale";
export type FilterOperator = FilterCondition["operator"];

/** A table row as returned by POST /v1/rows/query (people or companies scope). */
export interface LeadRow {
  id: string;
  company_id: string | null;
  membership_id?: string;
  added_at?: string;
  full_name?: string;
  first_name?: string | null;
  last_name?: string | null;
  title?: string | null;
  normalized_title?: string | null;
  seniority?: string | null;
  role_family?: string | null;
  decision_power?: number | null;
  profile_url?: string | null;
  person_confidence?: number | null;
  company?: string | null;
  domain?: string | null;
  website?: string | null;
  description?: string | null;
  city?: string | null;
  region?: string | null;
  country?: string | null;
  industry?: string | null;
  employee_min?: number | null;
  employee_max?: number | null;
  company_confidence?: number | null;
  phone?: string | null;
  email?: string | null;
  email_status?: EmailStatus | null;
  email_confidence?: number | null;
  email_checked_at?: string | null;
  icp_score?: number | null;
  overall_confidence?: number | null;
  qualified?: boolean | null;
  first_seen_at?: string;
  updated_at?: string;
  times_exported?: number;
  last_exported_at?: string | null;
  needs_review?: boolean;
  last_crawled_at?: string | null;
  registry_id?: string | null;
  people_count?: number;
  website_status?: string;
  sources: string[];
  cells: Record<string, Cell>;
}

export interface Cell {
  v: unknown;
  d: string | null;
  s: CellStatus;
  c: number | null;
  u?: boolean;
  e?: string | null;
}
