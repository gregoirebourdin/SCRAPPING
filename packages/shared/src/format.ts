const nf = new Intl.NumberFormat("en-US");

export function n(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : nf.format(v);
}

export function pct(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined) return "—";
  return `${(v * 100).toFixed(digits)}%`;
}

export function usd(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined) return "—";
  return `$${v.toFixed(digits)}`;
}

export function employees(min?: number | null, max?: number | null): string {
  if (min == null && max == null) return "—";
  if (min != null && max != null) return min === max ? `${min}` : `${min}–${max}`;
  return min != null ? `${min}+` : `≤${max}`;
}

export function relTime(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso).getTime();
  const s = Math.round((Date.now() - d) / 1000);
  if (s < 45) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  const days = Math.round(h / 24);
  if (days < 30) return `${days}d ago`;
  const mo = Math.round(days / 30);
  if (mo < 12) return `${mo}mo ago`;
  return `${Math.round(mo / 12)}y ago`;
}

export function shortDate(iso?: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

export function freshness(iso?: string | null): "fresh" | "30d" | "90d" | "stale" | null {
  if (!iso) return null;
  const days = (Date.now() - new Date(iso).getTime()) / 86400000;
  if (days < 7) return "fresh";
  if (days < 30) return "30d";
  if (days < 90) return "90d";
  return "stale";
}

export function hostname(url?: string | null): string {
  if (!url) return "";
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

export const SOURCE_LABELS: Record<string, string> = {
  website: "Website",
  registry: "Registry",
  fr_registry: "FR registry",
  google_maps: "Google Maps",
  maps: "Maps",
  web_search: "Web search",
  gemini_search: "Web research",
  grounded_search: "Web research",
  osm: "OpenStreetMap",
  yc: "Y Combinator",
  hn_hiring: "HN hiring",
  github: "GitHub",
  import: "Import",
  user: "You",
  ai_extraction: "AI extraction",
  tech_scan: "Tech scan",
  directory: "Directory",
  search_snippet: "Search",
  fixture: "Fixture",
  derived: "Derived",
};

export function sourceLabel(key: string): string {
  return SOURCE_LABELS[key] ?? key.replace(/_/g, " ");
}
