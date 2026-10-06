"use client";

import type { Cell, LeadRow } from "@scout/schemas";
import { type InfiniteData, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { toast } from "sonner";
import { create } from "zustand";

import { qk } from "@/lib/queries";

/* Live state pushed by the API over SSE (job_events). The table never reorders under the user:
   qualified leads increment a "+N new leads" counter (or are appended at the end when the view is unsorted);
   enrichment cells update in place with a soft flash; in-flight candidates feed skeleton rows. */

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
  /** client clock (ms) of the last event of any kind for this campaign — the heartbeat */
  lastEventAt?: number;
  /** client clock (ms) of the last event that moved a counter */
  lastProgressAt?: number;
  lastEventType?: string;
}

export interface LiveCandidate {
  event_id: string;
  name: string | null;
  domain: string | null;
  stage: string;
  at: number;
}

export type ConnState = "connecting" | "open" | "reconnecting";

interface LiveState {
  connected: boolean;
  connState: ConnState;
  /** when the stream dropped (ms), null while connected */
  disconnectedAt: number | null;
  newLeads: Record<string, number>;
  columnProgress: Record<string, { done: number; total: number; unknown?: number; failed?: number }>;
  campaigns: Record<string, CampaignLive>;
  /** in-flight candidates per campaign (skeleton rows, "what's happening now") */
  candidates: Record<string, Record<string, LiveCandidate>>;
  /** entity ids (person / company) delivered live → arrival time (row enter animation) */
  fresh: Record<string, number>;
  /** row id → column id → time of the last in-place update (cell flash) */
  flash: Record<string, Record<string, number>>;
  setConnected: (v: boolean) => void;
  setConn: (s: ConnState) => void;
  addNewLead: (listId: string) => void;
  clearNewLeads: (listId: string) => void;
  setColumnProgress: (id: string, p: LiveState["columnProgress"][string]) => void;
  patchCampaign: (id: string, p: CampaignLive) => void;
  upsertCandidate: (campaignId: string, c: LiveCandidate) => void;
  removeCandidate: (campaignId: string, eventId: string) => void;
  clearCandidates: (campaignId: string) => void;
  seedCandidates: (campaignId: string, list: LiveCandidate[]) => void;
  markFresh: (ids: string[]) => void;
  markFlash: (rowId: string, columnId: string) => void;
  prune: () => void;
}

const COUNTERS: (keyof CampaignLive)[] = ["qualified", "raw", "evaluated", "people", "emails", "safe"];
const FRESH_MS = 2200;
const FLASH_MS = 1600;

export const useLive = create<LiveState>()((set) => ({
  connected: false,
  connState: "connecting",
  disconnectedAt: null,
  newLeads: {},
  columnProgress: {},
  campaigns: {},
  candidates: {},
  fresh: {},
  flash: {},
  setConnected: (v) => set((s) => ({ connected: v, connState: v ? "open" : "reconnecting", disconnectedAt: v ? null : (s.disconnectedAt ?? Date.now()) })),
  setConn: (c) => set({ connState: c }),
  addNewLead: (listId) => set((s) => ({ newLeads: { ...s.newLeads, [listId]: (s.newLeads[listId] ?? 0) + 1 } })),
  clearNewLeads: (listId) => set((s) => ({ newLeads: { ...s.newLeads, [listId]: 0 } })),
  setColumnProgress: (id, p) => set((s) => ({ columnProgress: { ...s.columnProgress, [id]: p } })),
  patchCampaign: (id, p) =>
    set((s) => {
      const prev = s.campaigns[id] ?? {};
      const now = Date.now();
      const moved = COUNTERS.some((k) => p[k] !== undefined && p[k] !== prev[k]);
      return { campaigns: { ...s.campaigns, [id]: { ...prev, ...p, lastEventAt: now, lastProgressAt: moved ? now : (prev.lastProgressAt ?? now) } } };
    }),
  upsertCandidate: (cid, c) =>
    set((s) => ({
      candidates: { ...s.candidates, [cid]: { ...s.candidates[cid], [c.event_id]: c } },
      campaigns: { ...s.campaigns, [cid]: { ...s.campaigns[cid], lastEventAt: Date.now(), lastProgressAt: Date.now() } },
    })),
  removeCandidate: (cid, eid) =>
    set((s) => {
      const cur = s.candidates[cid];
      if (!cur || !cur[eid]) return { campaigns: { ...s.campaigns, [cid]: { ...s.campaigns[cid], lastEventAt: Date.now(), lastProgressAt: Date.now() } } };
      const next = { ...cur };
      delete next[eid];
      return {
        candidates: { ...s.candidates, [cid]: next },
        campaigns: { ...s.campaigns, [cid]: { ...s.campaigns[cid], lastEventAt: Date.now(), lastProgressAt: Date.now() } },
      };
    }),
  clearCandidates: (cid) => set((s) => ({ candidates: { ...s.candidates, [cid]: {} } })),
  seedCandidates: (cid, list) =>
    set((s) => {
      // server snapshot (after a refresh): keep live entries that are newer than the snapshot
      const merged: Record<string, LiveCandidate> = {};
      for (const c of list) merged[c.event_id] = c;
      for (const [k, v] of Object.entries(s.candidates[cid] ?? {})) merged[k] = v;
      return { candidates: { ...s.candidates, [cid]: merged } };
    }),
  markFresh: (ids) =>
    set((s) => {
      const now = Date.now();
      const next = { ...s.fresh };
      for (const id of ids) if (id) next[id] = now;
      return { fresh: next };
    }),
  markFlash: (rowId, columnId) => set((s) => ({ flash: { ...s.flash, [rowId]: { ...s.flash[rowId], [columnId]: Date.now() } } })),
  prune: () =>
    set((s) => {
      const now = Date.now();
      const fresh = Object.fromEntries(Object.entries(s.fresh).filter(([, t]) => now - t < FRESH_MS));
      const flash: LiveState["flash"] = {};
      for (const [row, cols] of Object.entries(s.flash)) {
        const keep = Object.fromEntries(Object.entries(cols).filter(([, t]) => now - t < FLASH_MS));
        if (Object.keys(keep).length) flash[row] = keep;
      }
      const sameFresh = Object.keys(fresh).length === Object.keys(s.fresh).length;
      const sameFlash =
        Object.keys(flash).length === Object.keys(s.flash).length && Object.entries(flash).every(([k, v]) => Object.keys(v).length === Object.keys(s.flash[k] ?? {}).length);
      return sameFresh && sameFlash ? s : { fresh: sameFresh ? s.fresh : fresh, flash: sameFlash ? s.flash : flash };
    }),
}));

interface EventEnvelope<T = Record<string, unknown>> {
  type: string;
  payload: T;
  campaign_id?: string | null;
  at?: string;
}

type RowsPage = { rows: LeadRow[]; next_cursor: string | null; total: number };

const TERMINAL = ["completed", "exhausted", "budget_reached", "limit_reached", "failed", "cancelled"];

export function useLiveEvents() {
  const qc = useQueryClient();
  useEffect(() => {
    let es: EventSource | null = null;
    let stopped = false;
    let retry = 1000;
    let lastId: string | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const throttles = new Map<string, number>();
    const throttle = (key: string, ms: number, fn: () => void) => {
      const now = Date.now();
      if ((throttles.get(key) ?? 0) + ms > now) return;
      throttles.set(key, now);
      fn();
    };
    const live = useLive.getState();
    const pruner = setInterval(() => useLive.getState().prune(), 700);

    function connect() {
      if (stopped) return;
      // Resume exactly after the last event we saw (a manual reconnect does not send Last-Event-ID).
      es = new EventSource(`/api/v1/events/stream${lastId ? `?since=${encodeURIComponent(lastId)}` : ""}`);
      es.addEventListener("ready", (ev) => {
        const wasDown = useLive.getState().disconnectedAt !== null && lastId !== null;
        retry = 1000;
        const id = (ev as MessageEvent).lastEventId;
        if (id) lastId = id;
        useLive.getState().setConnected(true);
        if (wasDown) {
          // anything that changed while we were away (beyond the replayed events) is re-read from the server
          void qc.invalidateQueries({ queryKey: qk.campaigns });
          void qc.invalidateQueries({ queryKey: ["campaign"] });
          void qc.invalidateQueries({ queryKey: ["campaign-live"] });
        }
      });
      es.onerror = () => {
        useLive.getState().setConnected(false);
        es?.close();
        if (!stopped) timer = setTimeout(connect, retry);
        retry = Math.min(retry * 2, 15000);
      };
      const on = (type: string, fn: (e: EventEnvelope) => void) =>
        es!.addEventListener(type, (ev) => {
          const me = ev as MessageEvent;
          if (me.lastEventId) lastId = me.lastEventId;
          try {
            fn(JSON.parse(me.data) as EventEnvelope);
          } catch {
            /* malformed frame: ignore */
          }
        });

      on("lead.qualified", (e) => {
        const listId = e.payload.list_id as string | undefined;
        const cid = (e.payload.campaign_id as string) ?? e.campaign_id;
        if (listId) live.addNewLead(listId);
        live.markFresh([e.payload.person_id as string, e.payload.company_id as string].filter(Boolean));
        if (cid) {
          const eventId = e.payload.event_id as string | undefined;
          if (eventId) live.removeCandidate(cid, eventId);
          const cur = useLive.getState().campaigns[cid];
          live.patchCampaign(cid, { qualified: (cur?.qualified ?? 0) + 1, lastEventType: "lead.qualified" });
          throttle(`campaign:${cid}`, 2500, () => qc.invalidateQueries({ queryKey: qk.campaign(cid) }));
        }
        throttle("lists", 3000, () => qc.invalidateQueries({ queryKey: qk.lists }));
        if (listId) throttle(`list:${listId}`, 3000, () => qc.invalidateQueries({ queryKey: qk.list(listId) }));
      });
      on("candidate.stage", (e) => {
        const cid = (e.payload.campaign_id as string) ?? e.campaign_id;
        if (!cid) return;
        live.upsertCandidate(cid, {
          event_id: e.payload.event_id as string,
          name: (e.payload.name as string) ?? null,
          domain: (e.payload.domain as string) ?? null,
          stage: e.payload.stage as string,
          at: Date.now(),
        });
      });
      on("candidate.done", (e) => {
        const cid = (e.payload.campaign_id as string) ?? e.campaign_id;
        if (cid) live.removeCandidate(cid, e.payload.event_id as string);
      });
      on("campaign.progress", (e) => {
        const id = (e.payload.campaign_id as string) ?? e.campaign_id;
        if (!id) return;
        live.patchCampaign(id, { ...(e.payload as CampaignLive), lastEventType: "campaign.progress" });
        throttle(`campaign:${id}`, 4000, () => qc.invalidateQueries({ queryKey: qk.campaign(id) }));
      });
      on("campaign.amended", (e) => {
        const id = (e.payload.campaign_id as string) ?? e.campaign_id;
        if (!id) return;
        live.patchCampaign(id, { target: e.payload.target as number, lastEventType: "campaign.amended" });
        void qc.invalidateQueries({ queryKey: qk.campaign(id) });
        void qc.invalidateQueries({ queryKey: ["campaign-live", id] });
      });
      on("campaign.status", (e) => {
        const id = (e.payload.campaign_id as string) ?? e.campaign_id;
        if (!id) return;
        const status = e.payload.status as string;
        const prev = useLive.getState().campaigns[id]?.status;
        live.patchCampaign(id, {
          status,
          reason: (e.payload.reason as string | undefined) ?? (status === "running" ? undefined : useLive.getState().campaigns[id]?.reason),
          lastEventType: "campaign.status",
        });
        if (TERMINAL.includes(status)) live.clearCandidates(id);
        void qc.invalidateQueries({ queryKey: qk.campaign(id) });
        void qc.invalidateQueries({ queryKey: ["campaign-live", id] });
        void qc.invalidateQueries({ queryKey: qk.campaigns });
        if (prev !== status && ["completed", "exhausted", "budget_reached", "limit_reached", "failed"].includes(status)) {
          toast(status === "completed" ? "Search completed" : "Search stopped", { description: (e.payload.reason as string) ?? undefined });
        }
      });
      on("cell.updated", (e) => {
        const columnId = e.payload.column_id as string;
        const cells =
          (e.payload.cells as { entity_type: string; entity_id: string; status: string; display_value: string | null; confidence: number | null; value?: unknown }[]) ?? [];
        if (!columnId || !cells.length) return;
        const byEntity = new Map(cells.map((c) => [`${c.entity_type}:${c.entity_id}`, c]));
        const flashed: string[] = [];
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
              if (hit.status !== "running" && hit.status !== "queued" && prev?.d !== next.d) flashed.push(row.id);
              return { ...row, cells: { ...row.cells, [columnId]: next } };
            }),
          }));
          return changed ? { ...data, pages } : data;
        });
        for (const rowId of flashed) live.markFlash(rowId, `cf:${columnId}`);
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
      clearInterval(pruner);
      if (timer) clearTimeout(timer);
      es?.close();
    };
  }, [qc]);
}
