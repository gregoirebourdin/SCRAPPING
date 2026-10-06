"use client";

import { useEffect, useMemo, useState } from "react";

import { type ConnState, type LiveCandidate, useLive } from "@/components/shell/live-events";
import { sourceLabel } from "@/lib/format";
import { type CampaignLiveSnapshot, useCampaign, useCampaignLive } from "@/lib/queries";

/* One view of a live run, shared by the table run header and the chat run card: server status (polled) +
   SSE patches + in-flight candidates + stall detection. Everything degrades gracefully: without SSE the
   10 s /live poll still drives the numbers and the stall check uses server clocks. */

export const ACTIVE = ["running", "planning"];
export const STOPPED = ["completed", "exhausted", "budget_reached", "limit_reached", "failed", "cancelled"];
export type Lang = "fr" | "en";
export type Stall = "none" | "slow" | "stuck";

export const STUCK_AFTER_S = 45;
export const SLOW_AFTER_S = 120;

export interface RunView {
  id: string;
  name: string;
  status: string;
  target: number;
  qualified: number;
  raw: number;
  evaluated: number;
  people: number;
  emails: number;
  inFlight: number;
  rate: number | null;
  eta: number | null;
  cost: number;
  maxCost: number | null;
  reason: string | null;
  listId: string | null;
  interpretation: { label: string; value: string }[];
  sources: string[];
  candidates: LiveCandidate[];
  stageCounts: Record<string, number>;
  health: CampaignLiveSnapshot["health"] | null;
  stall: Stall;
  silenceS: number | null;
  conn: ConnState;
  disconnectedAt: number | null;
  loaded: boolean;
  error: Error | null;
}

/** Re-render every `ms` while `on` (relative times, stall timers). */
export function useNow(ms: number, on = true): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!on) return;
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms, on]);
  return now;
}

const NO_CANDIDATES: Record<string, LiveCandidate> = Object.freeze({}) as Record<string, LiveCandidate>;

export function useRunView(id: string | null): RunView | null {
  const q = useCampaign(id);
  const snap = useCampaignLive(id);
  const live = useLive((s) => (id ? s.campaigns[id] : undefined));
  const cands = useLive((s) => (id ? (s.candidates[id] ?? NO_CANDIDATES) : NO_CANDIDATES));
  const conn = useLive((s) => s.connState);
  const disconnectedAt = useLive((s) => s.disconnectedAt);
  const status = live?.status ?? snap.data?.status ?? q.data?.status ?? "planning";
  const running = ACTIVE.includes(status);
  const now = useNow(5000, running);

  // a refresh mid-run: seed the skeleton rows from the server snapshot
  const snapFlight = snap.data?.in_flight;
  useEffect(() => {
    if (!id || !snapFlight) return;
    useLive.getState().seedCandidates(
      id,
      snapFlight.map((c) => ({ ...c, at: 0 })),
    );
  }, [id, snapFlight]);

  return useMemo(() => {
    if (!id) return null;
    const d = q.data ?? snap.data;
    const st = (d?.stats ?? {}) as Record<string, number>;
    const candidates = Object.values(cands)
      .filter((c) => c.stage !== "deliver")
      .sort((a, b) => b.at - a.at);
    const stageCounts: Record<string, number> = { ...(snap.data?.stages ?? {}) };
    if (candidates.some((c) => c.at > 0)) {
      for (const k of Object.keys(stageCounts)) if (k !== "discovered") delete stageCounts[k];
      for (const c of candidates) stageCounts[c.stage] = (stageCounts[c.stage] ?? 0) + 1;
    }
    // heartbeat: SSE when connected, otherwise the server's own clock
    const health = snap.data?.health ?? null;
    let silence: number | null = null;
    if (conn === "open" && live?.lastEventAt) silence = (now - live.lastEventAt) / 1000;
    if (health?.last_event_at && health.server_time) {
      const server = (Date.parse(health.server_time) - Date.parse(health.last_event_at)) / 1000 + (now - snap.dataUpdatedAt) / 1000;
      silence = silence === null ? server : Math.min(silence, server);
    }
    let stall: Stall = "none";
    if (running && silence !== null) {
      if (silence > STUCK_AFTER_S) stall = "stuck";
      else if (live?.lastProgressAt && (now - live.lastProgressAt) / 1000 > SLOW_AFTER_S) stall = "slow";
    }
    const qualified = Math.max(live?.qualified ?? 0, st.qualified ?? 0);
    return {
      id,
      name: d?.name ?? "Search",
      status,
      target: live?.target ?? d?.target ?? 0,
      qualified,
      raw: Math.max(live?.raw ?? 0, st.raw_discovered ?? 0),
      evaluated: Math.max(live?.evaluated ?? 0, st.companies_evaluated ?? 0),
      people: Math.max(live?.people ?? 0, st.people_found ?? 0),
      emails: Math.max(live?.emails ?? 0, st.emails_found ?? 0),
      inFlight: live?.in_flight ?? st.in_flight ?? candidates.length,
      rate: live?.rate_per_minute ?? d?.rate_per_minute ?? null,
      eta: live?.eta_minutes ?? d?.eta_minutes ?? null,
      cost: live?.cost_usd ?? d?.cost_usd ?? 0,
      maxCost: ((d?.definition as { limits?: { max_cost_usd?: number | null } } | undefined)?.limits?.max_cost_usd ?? null) as number | null,
      reason: (STOPPED.includes(status) ? (live?.reason ?? d?.stop_reason) : null) ?? null,
      listId: d?.target_list_id ?? null,
      interpretation: d?.interpretation ?? [],
      sources: (d?.sources ?? []).map((s) => s.key),
      candidates,
      stageCounts,
      health,
      stall,
      silenceS: silence,
      conn,
      disconnectedAt,
      loaded: Boolean(d),
      error: (q.error as Error | null) ?? null,
    };
  }, [id, q.data, q.error, snap.data, snap.dataUpdatedAt, live, cands, conn, disconnectedAt, now, status, running]);
}

/* ---- words ------------------------------------------------------------------------------------- */

const FR_SOURCES: Record<string, string> = {
  google_maps: "Google Maps",
  fr_registry: "annuaire",
  osm: "OpenStreetMap",
  web_search: "recherche web",
  gemini_search: "recherche IA",
  yc: "annuaire YC",
  fixture: "annuaire de démo",
};

export function sourceWord(key: string, lang: Lang): string {
  return lang === "fr" ? (FR_SOURCES[key] ?? sourceLabel(key)) : sourceLabel(key);
}

/** Stage of a candidate as it is *being* worked on (the server records the stage just completed). */
export function candidateStage(stage: string, lang: Lang): string {
  const fr = lang === "fr";
  switch (stage) {
    case "discovered":
      return fr ? "En file d'attente" : "Queued";
    case "website":
      return fr ? "Analyse du site" : "Reading the website";
    case "crawl":
    case "website_conditions":
      return fr ? "Vérification des critères" : "Checking criteria";
    case "company_qualification":
      return fr ? "Recherche des dirigeants" : "Finding decision-makers";
    case "people":
      return fr ? "Vérification de l'email" : "Verifying the email";
    default:
      return fr ? "En cours" : "In progress";
  }
}

function plural(n: number, one: string, many: string) {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`;
}

export interface StageLine {
  text: string;
  active: boolean;
  tone: "accent" | "success" | "warning" | "danger" | "muted";
}

/** The one sentence that says where the run is ("Analyse de 14 sites…"). */
export function stageLine(r: RunView, lang: Lang): StageLine {
  const fr = lang === "fr";
  const q = r.qualified.toLocaleString();
  const t = r.target.toLocaleString();
  switch (r.status) {
    case "planning":
    case "draft":
      return { text: fr ? "Je prépare les sources…" : "Preparing sources…", active: true, tone: "accent" };
    case "paused":
      return { text: fr ? "En pause — tout ce qui a été trouvé est conservé" : "Paused — everything found so far is kept", active: false, tone: "warning" };
    case "completed":
      return { text: fr ? `Terminé — ${q} leads qualifiés` : `Completed — ${q} qualified leads`, active: false, tone: "success" };
    case "exhausted":
      return { text: fr ? `Plus de résultats pour ces critères — ${q} / ${t}` : `No more results for these criteria — ${q} / ${t}`, active: false, tone: "warning" };
    case "budget_reached":
      return { text: fr ? `Budget atteint — ${q} / ${t}` : `Budget reached — ${q} / ${t}`, active: false, tone: "warning" };
    case "limit_reached":
      return { text: fr ? `Limite de sécurité atteinte — ${q} / ${t}` : `Safety limit reached — ${q} / ${t}`, active: false, tone: "warning" };
    case "failed":
      return { text: fr ? `Échec — ${r.reason ?? "erreur inconnue"}` : `Failed — ${r.reason ?? "unknown error"}`, active: false, tone: "danger" };
    case "cancelled":
      return { text: fr ? `Arrêtée — ${q} leads conservés` : `Cancelled — ${q} leads kept`, active: false, tone: "muted" };
  }
  if (r.stall === "stuck") {
    return { text: fr ? "Ça semble bloqué…" : "Looks stuck…", active: false, tone: "danger" };
  }
  const sc = r.stageCounts;
  const reading = sc.website ?? 0;
  const criteria = (sc.crawl ?? 0) + (sc.website_conditions ?? 0);
  const people = sc.company_qualification ?? 0;
  const emails = sc.people ?? 0;
  const queued = sc.discovered ?? 0;
  const groups: [number, string][] = [
    [emails, fr ? `Vérification des emails (${emails})…` : `Verifying emails (${emails})…`],
    [people, fr ? `Recherche des dirigeants (${plural(people, "entreprise", "entreprises")})…` : `Finding decision-makers at ${plural(people, "company", "companies")}…`],
    [criteria, fr ? `Vérification des critères sur ${plural(criteria, "site", "sites")}…` : `Checking criteria on ${plural(criteria, "site", "sites")}…`],
    [reading, fr ? `Analyse de ${plural(reading, "site", "sites")}…` : `Analysing ${plural(reading, "website", "websites")}…`],
  ];
  const best = groups.reduce((a, b) => (b[0] > a[0] ? b : a), [0, ""] as [number, string]);
  if (best[0] > 0) return { text: best[1], active: true, tone: "accent" };
  if (r.raw === 0) {
    const what = r.interpretation.find((i) => i.label === "Companies")?.value?.split(",")[0]?.toLowerCase();
    const where = r.interpretation.find((i) => i.label === "Location")?.value?.split(",")[0];
    const srcs = r.sources.slice(0, 3).map((s) => sourceWord(s, lang)).join(", ");
    const subject = what ? (fr ? `de ${what}` : what) : fr ? "d'entreprises" : "companies";
    const text = fr
      ? `Recherche ${subject}${where ? ` à ${where}` : ""}${srcs ? ` (${srcs})` : ""}…`
      : `Searching ${subject}${where ? ` in ${where}` : ""}${srcs ? ` (${srcs})` : ""}…`;
    return { text, active: true, tone: "accent" };
  }
  if (queued > 0) return { text: fr ? `${plural(queued, "entreprise", "entreprises")} en file d'attente…` : `${plural(queued, "company", "companies")} queued…`, active: true, tone: "accent" };
  return { text: fr ? "Recherche de nouvelles entreprises…" : "Looking for more companies…", active: true, tone: "accent" };
}

export function fmtSilence(s: number | null, lang: Lang): string {
  if (s === null) return "";
  const v = Math.round(s);
  if (v < 90) return lang === "fr" ? `${v} s` : `${v}s`;
  return lang === "fr" ? `${Math.round(v / 60)} min` : `${Math.round(v / 60)} min`;
}

/** Copy for the primary recovery action of a stopped run. */
export function recovery(status: string, lang: Lang): { label: string; mode: AmendMode } | null {
  const fr = lang === "fr";
  switch (status) {
    case "paused":
      return { label: fr ? "Reprendre avec des modifs…" : "Resume with changes…", mode: "resume" };
    case "exhausted":
      return { label: fr ? "Élargir la recherche…" : "Broaden the search…", mode: "broaden" };
    case "budget_reached":
      return { label: fr ? "Augmenter le budget…" : "Raise budget…", mode: "budget" };
    case "completed":
      return { label: fr ? "Trouver plus de leads…" : "Find more leads…", mode: "target" };
    case "limit_reached":
      return { label: fr ? "Prolonger…" : "Extend…", mode: "runtime" };
    default:
      return null;
  }
}

export type AmendMode = "resume" | "broaden" | "budget" | "target" | "runtime" | "adjust";
