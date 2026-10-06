"use client";

import type { ColumnOut, ListOut, MeOut, ViewOut } from "@scout/schemas";
import { useQuery } from "@tanstack/react-query";

import { api } from "./api";

export const qk = {
  me: ["me"] as const,
  lists: ["lists"] as const,
  list: (id: string) => ["list", id] as const,
  rows: (scope: string) => ["rows", scope] as const,
  views: (scope: string) => ["views", scope] as const,
  columns: (listId: string | null) => ["columns", listId ?? "workspace"] as const,
  fields: (entity: string, listId: string | null) => ["fields", entity, listId ?? "none"] as const,
  campaigns: ["campaigns"] as const,
  campaign: (id: string) => ["campaign", id] as const,
  person: (id: string) => ["person", id] as const,
  company: (id: string) => ["company", id] as const,
  history: (entity: string, id: string) => ["history", entity, id] as const,
  activity: ["activity"] as const,
  usage: ["usage"] as const,
  sources: ["sources"] as const,
  threads: (listId: string | null) => ["threads", listId ?? "all"] as const,
  thread: (id: string) => ["thread", id] as const,
};

export function useMe() {
  return useQuery({ queryKey: qk.me, queryFn: () => api<MeOut>("me"), staleTime: 60_000 });
}

export function useLists() {
  return useQuery({ queryKey: qk.lists, queryFn: () => api<ListOut[]>("lists"), staleTime: 10_000 });
}

export interface ListDetail extends ListOut {
  summary: {
    total: number;
    safe_emails?: number;
    with_email?: number;
    decision_maker_confidence?: number | null;
    avg_icp_score?: number | null;
    boolean_columns: { column_id: string; name: string; true_count: number }[];
  };
}

export function useList(id: string | null) {
  return useQuery({
    queryKey: qk.list(id ?? "none"),
    queryFn: () => api<ListDetail>(`lists/${id}`),
    enabled: Boolean(id),
    staleTime: 5_000,
  });
}

export function useViews(listId: string | null, entityType: string) {
  const scope = listId ? `list:${listId}` : entityType;
  return useQuery({
    queryKey: qk.views(scope),
    queryFn: () => api<ViewOut[]>(`views?${listId ? `list_id=${listId}` : `entity_type=${entityType}`}`),
    staleTime: 30_000,
  });
}

export interface CampaignSummary {
  id: string;
  name: string;
  status: string;
  target: number;
  qualified: number;
  raw: number;
  cost_usd: number;
  target_list_id: string | null;
  created_at: string;
  stopped_at: string | null;
  stop_reason: string | null;
  mode: string;
}

export function useCampaigns() {
  return useQuery({ queryKey: qk.campaigns, queryFn: () => api<CampaignSummary[]>("campaigns"), staleTime: 5_000 });
}

export interface CampaignStatus {
  id: string;
  name: string;
  status: string;
  stop_reason: string | null;
  prompt: string | null;
  mode: string;
  target: number;
  target_list_id: string | null;
  interpretation: { label: string; value: string }[];
  definition: Record<string, unknown>;
  created_at: string;
  started_at: string | null;
  stopped_at: string | null;
  stats: Record<string, number>;
  cost_usd: number;
  cost_per_qualified: number | null;
  rate_per_minute: number | null;
  eta_minutes: number | null;
  sources_exhausting: boolean;
  sources: { key: string; status: string; priority: number; raw: number; unique: number; qualified: number; errors: number; last_error: string | null }[];
  top_rejections: { reason: string; count: number }[];
}

export function useCampaign(id: string | null, live = true) {
  return useQuery({
    queryKey: qk.campaign(id ?? "none"),
    queryFn: () => api<CampaignStatus>(`campaigns/${id}`),
    enabled: Boolean(id),
    refetchInterval: (q) => (live && q.state.data && ["running", "planning"].includes(q.state.data.status) ? 4_000 : false),
  });
}

export function useColumns(listId: string | null, entityType?: string) {
  return useQuery({
    queryKey: [...qk.columns(listId), entityType ?? "any"],
    queryFn: () => api<ColumnOut[]>(`columns?${new URLSearchParams({ ...(listId ? { list_id: listId } : {}), ...(entityType ? { entity_type: entityType } : {}) })}`),
    staleTime: 15_000,
  });
}

/* ---- live runs (additive) ------------------------------------------------------------------------ */

export interface CampaignLiveSnapshot extends CampaignStatus {
  stages: Record<string, number>;
  in_flight: { event_id: string; name: string | null; domain: string | null; stage: string }[];
  recent: { id: number; type: string; payload: Record<string, unknown>; at: string }[];
  health: {
    server_time: string;
    last_event_at: string | null;
    last_progress_at: string | null;
    jobs: Record<string, number>;
    overdue_jobs: number;
    oldest_overdue_s: number | null;
    last_error: string | null;
    workers_enabled: boolean;
  };
}

const LIVE_STATUSES = ["running", "planning"];

/** Status + in-flight candidates + stall diagnostics; polls while the run is active (SSE does the rest). */
export function useCampaignLive(id: string | null) {
  return useQuery({
    queryKey: ["campaign-live", id ?? "none"],
    queryFn: () => api<CampaignLiveSnapshot>(`campaigns/${id}/live`),
    enabled: Boolean(id),
    refetchInterval: (q) => (q.state.data && LIVE_STATUSES.includes(q.state.data.status) ? 10_000 : false),
    staleTime: 3_000,
  });
}

export interface AmendChange {
  field: string;
  label: string;
  before: string;
  after: string;
  safe: boolean;
}

export interface AmendResult {
  campaign_id: string;
  status: string;
  changes: AmendChange[];
  warnings: string[];
  requires_pause: boolean;
  noop: boolean;
  base_hash: string;
  applied: boolean;
  resumed?: boolean;
  auto_paused?: boolean;
  resume_blocked?: { code: string; message: string; hint?: string | null } | null;
  new_queries?: number;
  interpretation?: { label: string; value: string }[];
}

export interface AmendBody {
  instruction?: string | null;
  add_target?: number | null;
  target_qualified_count?: number | null;
  max_cost_usd?: number | null;
  max_runtime_hours?: number | null;
  dry_run?: boolean;
  resume?: boolean;
  base_hash?: string | null;
}

export function amendCampaign(id: string, body: AmendBody, signal?: AbortSignal) {
  return api<AmendResult>(`campaigns/${id}`, { method: "PATCH", body, signal });
}

export type CampaignAction = "pause" | "resume" | "retry" | "kick" | "cancel";

export function campaignAction(id: string, action: CampaignAction) {
  return api<CampaignStatus>(`campaigns/${id}/${action}`, { method: "POST" });
}
