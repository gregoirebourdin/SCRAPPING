"use client";

import type { Cell, LeadRow } from "@scout/schemas";
import { type InfiniteData, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { toast } from "sonner";
import { create } from "zustand";

import { qk } from "@/lib/queries";

/* Live state pushed by the API over SSE (job_events). The table never reorders under the user:
   qualified leads increment a "+N new leads" counter; enrichment cells update in place. */

export interface CampaignLive {
  qualified?: number;
  target?: number;
  raw?: number;
  evaluated?: number;
  people?: number;
  emails?: number;
  safe?: number;
  excluded?: number;
  duplicates?: number;
  in_flight?: number;
  rate_per_minute?: number | null;
  eta_minutes?: number | null;
  cost_usd?: number;
  status?: string;
  reason?: string;
  exhausting?: boolean;
}

interface LiveState {
  connected: boolean;
  newLeads: Record<string, number>;
  columnProgress: Record<string, { done: number; total: number; unknown?: number; failed?: number }>;
  campaigns: Record<string, CampaignLive>;
  setConnected: (v: boolean) => void;
  addNewLead: (listId: string) => void;
  clearNewLeads: (listId: string) => void;
  setColumnProgress: (id: string, p: LiveState["columnProgress"][string]) => void;
  patchCampaign: (id: string, p: CampaignLive) => void;
}

export const useLive = create<LiveState>()((set) => ({
  connected: false,
  newLeads: {},
  columnProgress: {},
  campaigns: {},
  setConnected: (v) => set({ connected: v }),
  addNewLead: (listId) => set((s) => ({ newLeads: { ...s.newLeads, [listId]: (s.newLeads[listId] ?? 0) + 1 } })),
  clearNewLeads: (listId) => set((s) => ({ newLeads: { ...s.newLeads, [listId]: 0 } })),
  setColumnProgress: (id, p) => set((s) => ({ columnProgress: { ...s.columnProgress, [id]: p } })),
  patchCampaign: (id, p) => set((s) => ({ campaigns: { ...s.campaigns, [id]: { ...s.campaigns[id], ...p } } })),
}));

interface EventEnvelope<T = Record<string, unknown>> {
  type: string;
  payload: T;
  campaign_id?: string | null;
  at?: string;
}

type RowsPage = { rows: LeadRow[]; next_cursor: string | null; total: number };

export function useLiveEvents() {
  const qc = useQueryClient();
  useEffect(() => {
    let es: EventSource | null = null;
    let stopped = false;
    let retry = 1000;
    const throttles = new Map<string, number>();
    const throttle = (key: string, ms: number, fn: () => void) => {
      const now = Date.now();
      if ((throttles.get(key) ?? 0) + ms > now) return;
      throttles.set(key, now);
      fn();
    };
    const live = useLive.getState();

    function connect() {
      if (stopped) return;
      es = new EventSource("/api/v1/events/stream");
      es.addEventListener("ready", () => {
        retry = 1000;
        useLive.getState().setConnected(true);
      });
      es.onerror = () => {
        useLive.getState().setConnected(false);
        es?.close();
        if (!stopped) setTimeout(connect, retry);
        retry = Math.min(retry * 2, 15000);
      };
      const on = (type: string, fn: (e: EventEnvelope) => void) =>
        es!.addEventListener(type, (ev) => {
          try {
            fn(JSON.parse((ev as MessageEvent).data) as EventEnvelope);
          } catch {
            /* malformed frame: ignore */
          }
        });

      on("lead.qualified", (e) => {
        const listId = e.payload.list_id as string | undefined;
        if (listId) live.addNewLead(listId);
        throttle("lists", 3000, () => qc.invalidateQueries({ queryKey: qk.lists }));
        if (listId) throttle(`list:${listId}`, 3000, () => qc.invalidateQueries({ queryKey: qk.list(listId) }));
      });
      on("campaign.progress", (e) => {
        const id = (e.payload.campaign_id as string) ?? e.campaign_id;
        if (!id) return;
        live.patchCampaign(id, e.payload as CampaignLive);
        throttle(`campaign:${id}`, 4000, () => qc.invalidateQueries({ queryKey: qk.campaign(id) }));
      });
      on("campaign.status", (e) => {
        const id = (e.payload.campaign_id as string) ?? e.campaign_id;
        if (!id) return;
        live.patchCampaign(id, { status: e.payload.status as string, reason: e.payload.reason as string | undefined });
        qc.invalidateQueries({ queryKey: qk.campaign(id) });
        qc.invalidateQueries({ queryKey: qk.campaigns });
        const status = e.payload.status as string;
        if (["completed", "exhausted", "budget_reached", "limit_reached", "failed"].includes(status)) {
          toast(status === "completed" ? "Campaign completed" : "Campaign stopped", { description: (e.payload.reason as string) ?? undefined });
        }
      });
      on("cell.updated", (e) => {
        const columnId = e.payload.column_id as string;
        const cells =
          (e.payload.cells as { entity_type: string; entity_id: string; status: string; display_value: string | null; confidence: number | null; value?: unknown }[]) ?? [];
        if (!columnId || !cells.length) return;
        const byEntity = new Map(cells.map((c) => [`${c.entity_type}:${c.entity_id}`, c]));
        qc.setQueriesData<InfiniteData<RowsPage>>({ queryKey: ["rows"] }, (data) => {
          if (!data) return data;
          let changed = false;
          const pages = data.pages.map((page) => ({
            ...page,
            rows: page.rows.map((row) => {
              const hit = byEntity.get(`company:${row.company_id}`) ?? byEntity.get(`person:${row.id}`);
              if (!hit) return row;
              changed = true;
              const prev: Cell | undefined = row.cells[columnId];
              const next: Cell = {
                v: hit.value !== undefined ? hit.value : hit.display_value === "true" ? true : hit.display_value === "false" ? false : hit.display_value,
                d: hit.display_value,
                s: hit.status as Cell["s"],
                c: hit.confidence,
                u: prev?.u,
                e: prev?.e,
              };
              return { ...row, cells: { ...row.cells, [columnId]: next } };
            }),
          }));
          return changed ? { ...data, pages } : data;
        });
      });
      on("column.progress", (e) => {
        const p = e.payload as { column_id: string; done: number; total: number; unknown?: number; failed?: number };
        if (p.column_id) live.setColumnProgress(p.column_id, { done: p.done, total: p.total, unknown: p.unknown, failed: p.failed });
      });
      on("import.progress", (e) => {
        const p = e.payload as { status: string; imported: number; merged: number; skipped: number; errors: number; done: number; total: number };
        if (p.status === "completed") {
          toast.success("Import completed", { description: `${p.imported} new · ${p.merged} merged · ${p.skipped} skipped · ${p.errors} errors` });
          qc.invalidateQueries({ queryKey: qk.lists });
          qc.invalidateQueries({ queryKey: ["rows"] });
        }
      });
      on("company.refreshed", () => {
        qc.invalidateQueries({ queryKey: ["company"] });
        qc.invalidateQueries({ queryKey: ["person"] });
      });
    }
    connect();
    return () => {
      stopped = true;
      es?.close();
    };
  }, [qc]);
}
