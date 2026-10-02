export const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type Lead = {
  id: number;
  domain: string;
  website: string;
  name: string | null;
  tagline: string | null;
  country: string | null;
  city: string | null;
  language: string | null;
  services: string[];
  icp_signals: string[];
  tech: string[];
  score: number;
  tier?: string | null;
  status: string;
  user_status: string | null;
  alive: boolean;
  last_activity: string | null;
  created_at: string;
  primary_email: string | null;
  email_verification: string | null;
  email_count: number;
  client_count: number;
  top_client: string | null;
  top_client_role: string | null;
  top_client_website: string | null;
  top_funnel_url: string | null;
  top_funnel_type: string | null;
  top_funnel_platform: string | null;
  founder_name?: string | null;
  founder_title?: string | null;
  founder_linkedin?: string | null;
};

export type Funnel = {
  id: number;
  url: string;
  funnel_type: string;
  platform: string | null;
  offer: string | null;
  price_hint: string | null;
  steps: { step: string; evidence?: string }[];
  confidence: number;
};

export type Client = {
  id: number;
  name: string;
  kind: string;
  role_title: string | null;
  niche: string | null;
  evidence: string | null;
  evidence_url: string | null;
  website: string | null;
  website_source: string | null;
  confidence: number;
  status: string;
  funnels: Funnel[];
};

export type Email = {
  id: number;
  email: string;
  source: string;
  page_url: string | null;
  verification: string;
  is_primary: boolean;
  confidence: number;
};

export type LeadDetail = Lead & {
  description: string | null;
  founded_year: number | null;
  team_size_hint: string | null;
  socials: Record<string, string>;
  phones: string[];
  booking_url: string | null;
  alive_details: Record<string, unknown>;
  pages_crawled: number;
  key_pages: Record<string, string>;
  score_breakdown: Record<string, unknown>;
  reject_reason: string | null;
  notes: string | null;
  emails: Email[];
  clients: Client[];
};

export type Page<T> = { items: T[]; total: number; page: number; size: number };

export type Run = {
  id: number;
  name: string;
  status: string;
  stage: string;
  config: Record<string, unknown>;
  stats: Record<string, unknown>;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};

export type Stats = {
  candidates: number;
  candidates_by_status: Record<string, number>;
  agencies: number;
  agencies_by_status: Record<string, number>;
  qualified: number;
  with_email: number;
  with_verified_email: number;
  with_client: number;
  with_funnel: number;
  avg_score: number;
  engines: Record<string, Record<string, unknown>>;
  active_run: Run | null;
};

async function j<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.detail) msg = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* ignore */
    }
    throw new Error(msg);
  }
  return res.json() as Promise<T>;
}

export const api = {
  leads: (params: Record<string, string | number | boolean | undefined>) => {
    const q = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== "" && v !== null) q.set(k, String(v));
    });
    return fetch(`${API}/api/leads?${q}`).then((r) => j<Page<Lead>>(r));
  },
  lead: (id: number) => fetch(`${API}/api/leads/${id}`).then((r) => j<LeadDetail>(r)),
  patchLead: (id: number, body: { user_status?: string; notes?: string; status?: string }) =>
    fetch(`${API}/api/leads/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then((r) => j<LeadDetail>(r)),
  stats: () => fetch(`${API}/api/stats`).then((r) => j<Stats>(r)),
  runs: () => fetch(`${API}/api/runs`).then((r) => j<Run[]>(r)),
  startRun: (body: Record<string, unknown>) =>
    fetch(`${API}/api/runs`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then((r) => j<Run>(r)),
  cancelRun: (id: number) => fetch(`${API}/api/runs/${id}/cancel`, { method: "POST" }).then((r) => j<Run>(r)),
  exportUrl: (params: Record<string, string | undefined>) => {
    const q = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => v && q.set(k, v));
    return `${API}/api/export.csv?${q}`;
  },
};
