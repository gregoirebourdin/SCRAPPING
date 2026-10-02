"use client";

export function Badge({ children, tone = "zinc" }: { children: React.ReactNode; tone?: "zinc" | "green" | "amber" | "red" | "blue" | "violet" }) {
  const tones: Record<string, string> = {
    zinc: "bg-zinc-100 text-zinc-700",
    green: "bg-emerald-100 text-emerald-800",
    amber: "bg-amber-100 text-amber-800",
    red: "bg-rose-100 text-rose-800",
    blue: "bg-sky-100 text-sky-800",
    violet: "bg-violet-100 text-violet-800",
  };
  return <span className={`badge ${tones[tone]}`}>{children}</span>;
}

export function TierBadge({ tier }: { tier?: string | null }) {
  if (!tier) return <Badge>—</Badge>;
  const tone = tier === "A" ? "green" : tier === "B" ? "blue" : "amber";
  return <Badge tone={tone}>Tier {tier}</Badge>;
}

export function VerifBadge({ v }: { v?: string | null }) {
  if (!v) return null;
  const tone = v === "smtp_valid" ? "green" : v === "catch_all" || v === "mx_valid" ? "blue" : v === "unknown" ? "zinc" : "red";
  return <Badge tone={tone}>{v}</Badge>;
}

export function ScoreBar({ score }: { score: number }) {
  const color = score >= 75 ? "bg-emerald-500" : score >= 55 ? "bg-sky-500" : "bg-amber-500";
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-14 overflow-hidden rounded bg-zinc-200">
        <div className={`h-full ${color}`} style={{ width: `${Math.min(100, score)}%` }} />
      </div>
      <span className="tabular-nums text-xs text-zinc-600">{score}</span>
    </div>
  );
}

export function Ext({ href, children, className = "" }: { href: string | null | undefined; children?: React.ReactNode; className?: string }) {
  if (!href) return <span className="text-zinc-400">—</span>;
  return (
    <a href={href} target="_blank" rel="noreferrer" className={`text-sky-700 hover:underline ${className}`} onClick={(e) => e.stopPropagation()}>
      {children ?? href.replace(/^https?:\/\/(www\.)?/, "").replace(/\/$/, "")}
    </a>
  );
}

export function Button({ children, onClick, tone = "zinc", disabled, type = "button" }: { children: React.ReactNode; onClick?: () => void; tone?: "zinc" | "primary" | "danger"; disabled?: boolean; type?: "button" | "submit" }) {
  const tones = {
    zinc: "border-zinc-300 bg-white text-zinc-800 hover:bg-zinc-100",
    primary: "border-zinc-900 bg-zinc-900 text-white hover:bg-zinc-700",
    danger: "border-rose-300 bg-white text-rose-700 hover:bg-rose-50",
  };
  return (
    <button type={type} disabled={disabled} onClick={onClick} className={`rounded-md border px-3 py-1.5 text-sm font-medium disabled:opacity-50 ${tones[tone]}`}>
      {children}
    </button>
  );
}
