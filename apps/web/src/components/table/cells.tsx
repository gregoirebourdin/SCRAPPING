"use client";

import { Badge, cn, ConfidenceMeter, EmailStatusBadge, Spinner, Tip } from "@scout/design-system";
import type { Cell, ColumnOut, LeadRow } from "@scout/schemas";
import { Check, Copy, ExternalLink, Minus, Pencil } from "lucide-react";
import { toast } from "sonner";

import { employees, freshness, hostname, relTime, sourceLabel } from "@/lib/format";

export interface ColumnSpec {
  id: string;
  label: string;
  width: number;
  minWidth?: number;
  sortField?: string;
  filterField?: string;
  custom?: ColumnOut;
  editable?: "person" | "company" | "cell";
  editField?: string;
  copyValue?: (r: LeadRow) => string | null | undefined;
  render: (r: LeadRow) => React.ReactNode;
}

function initials(name?: string | null): string {
  if (!name) return "?";
  const parts = name.trim().split(/\s+/);
  return ((parts[0]?.[0] ?? "") + (parts.length > 1 ? parts[parts.length - 1]![0] : "")).toUpperCase();
}

// Monochrome: a few foreground intensities keep neighbours distinguishable without colour.
const AVATAR_MIX = [9, 13, 17, 22];

function avatarMix(seed: string): number {
  let h = 0;
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) | 0;
  return AVATAR_MIX[Math.abs(h) % AVATAR_MIX.length]!;
}

export function Avatar({ name, size = 18 }: { name?: string | null; size?: number }) {
  const mix = avatarMix(name ?? "?");
  return (
    <span
      className="inline-grid shrink-0 place-items-center rounded-full font-medium text-fg-2"
      style={{ width: size, height: size, fontSize: size * 0.42, background: `color-mix(in srgb, var(--text-primary) ${mix}%, transparent)` }}
      aria-hidden
    >
      {initials(name)}
    </span>
  );
}

function Muted({ children = "—" }: { children?: React.ReactNode }) {
  return <span className="text-fg-3">{children}</span>;
}

export function CopyButton({ value }: { value: string }) {
  return (
    <button
      type="button"
      tabIndex={-1}
      onClick={(e) => {
        e.stopPropagation();
        void navigator.clipboard.writeText(value);
        toast("Copied", { description: value, duration: 1500 });
      }}
      className="invisible rounded-xs p-0.5 text-fg-3 hover:bg-surface-3 hover:text-fg group-hover/cell:visible"
      aria-label="Copy"
    >
      <Copy className="size-3" />
    </button>
  );
}

function scoreTone(v: number): string {
  if (v >= 85) return "var(--success)";
  if (v >= 70) return "var(--accent)";
  if (v >= 55) return "var(--warning)";
  return "var(--danger)";
}

export function ScoreCell({ value }: { value?: number | null }) {
  if (value === null || value === undefined) return <Muted />;
  return (
    <span className="inline-flex items-center gap-2" title={`ICP score ${value}/100`}>
      <span className="tabular w-6 text-right font-medium text-fg">{value}</span>
      <span className="h-1 w-10 overflow-hidden rounded-full bg-surface-3">
        <span className="block h-full rounded-full" style={{ width: `${value}%`, background: scoreTone(value) }} />
      </span>
    </span>
  );
}

export function SourcesCell({ sources }: { sources: string[] }) {
  if (!sources?.length) return <Muted />;
  const shown = sources.slice(0, 2);
  return (
    <span className="flex min-w-0 items-center gap-1" title={sources.map(sourceLabel).join(", ")}>
      {shown.map((s) => (
        <Badge key={s} tone="neutral" className="h-4 px-1 text-micro">
          {sourceLabel(s)}
        </Badge>
      ))}
      {sources.length > 2 && <span className="text-micro text-fg-3">+{sources.length - 2}</span>}
    </span>
  );
}

export function UpdatedCell({ iso }: { iso?: string | null }) {
  const f = freshness(iso);
  const tone = f === "fresh" ? "bg-success" : f === "stale" ? "bg-warning" : "bg-fg-3";
  return (
    <span className="inline-flex items-center gap-1.5 text-fg-2" title={iso ? new Date(iso).toLocaleString() : undefined}>
      {f && <span className={cn("size-1.5 rounded-full", tone, f !== "fresh" && "opacity-60")} />}
      <span className="tabular">{relTime(iso)}</span>
    </span>
  );
}

/** Custom enrichment cell: EMPTY / QUEUED / RUNNING / DONE / UNKNOWN / ERROR (spec §125) + TRUE/FALSE/UNKNOWN (§155). */
export function CustomCell({ cell, col }: { cell?: Cell; col: ColumnOut }) {
  if (!cell || cell.s === "not_started") return <span className="text-fg-3/60">·</span>;
  if (cell.s === "queued")
    return (
      <span className="inline-flex items-center gap-1.5 text-meta text-fg-3">
        <span className="size-1.5 rounded-full border border-fg-3" />
        Queued
      </span>
    );
  if (cell.s === "running")
    return (
      <span className="inline-flex items-center gap-1.5 text-meta text-fg-3">
        <Spinner size={11} />
        Running
      </span>
    );
  if (cell.s === "failed")
    return (
      <Tip content={cell.e ?? "Enrichment failed"}>
        <span className="inline-flex items-center gap-1.5 text-meta text-danger">
          <span className="size-1.5 rounded-full bg-danger" />
          {shortError(cell.e)}
        </span>
      </Tip>
    );
  const stale = cell.s === "stale";
  const conf = cell.c != null ? `${Math.round(cell.c * 100)}% confidence` : undefined;
  const override = cell.u ? <Pencil className="size-2.5 shrink-0 text-accent" aria-label="Edited by you" /> : null;
  if (cell.s === "unknown" || cell.d === "unknown") {
    return (
      <span className="inline-flex items-center gap-1 text-meta italic text-fg-3" title="Insufficient evidence">
        Unknown{override}
      </span>
    );
  }
  if (col.data_type === "boolean") {
    const v = cell.d === "true" || cell.v === true;
    return (
      <span className={cn("inline-flex items-center gap-1", stale && "opacity-60")} title={conf}>
        {v ? (
          <span className="inline-flex items-center gap-1 text-success">
            <Check className="size-3.5" />
            Yes
          </span>
        ) : (
          <span className="inline-flex items-center gap-1 text-fg-2">
            <Minus className="size-3.5 text-fg-3" />
            No
          </span>
        )}
        {override}
      </span>
    );
  }
  const text = cell.d ?? (typeof cell.v === "string" ? cell.v : cell.v != null ? JSON.stringify(cell.v) : "");
  if (col.data_type === "url" && text) {
    return (
      <a
        href={text}
        target="_blank"
        rel="noreferrer noopener"
        onClick={(e) => e.stopPropagation()}
        className={cn("inline-flex min-w-0 items-center gap-1 text-accent-strong hover:underline", stale && "opacity-60")}
        title={conf}
      >
        <span className="truncate">{hostname(text) + (new URL(text, "https://x").pathname !== "/" ? new URL(text, "https://x").pathname : "")}</span>
      </a>
    );
  }
  return (
    <span className={cn("flex min-w-0 items-center gap-1", stale && "opacity-60", col.kind === "generated" && "text-fg-2")} title={[text, conf].filter(Boolean).join(" · ")}>
      <span className="truncate">{text || "—"}</span>
      {override}
    </span>
  );
}

function shortError(e?: string | null): string {
  if (!e) return "Error";
  return e.length > 28 ? `${e.slice(0, 26)}…` : e;
}

export function personColumns(): ColumnSpec[] {
  return [
    {
      id: "full_name",
      label: "Person",
      width: 210,
      minWidth: 140,
      sortField: "full_name",
      filterField: "full_name",
      editable: "person",
      editField: "full_name",
      copyValue: (r) => r.full_name,
      render: (r) => (
        <span className="flex min-w-0 items-center gap-2">
          <Avatar name={r.full_name} />
          <span className="truncate font-medium text-fg">{r.full_name}</span>
          {r.needs_review && <span className="size-1.5 shrink-0 rounded-full bg-warning" title="Needs review" />}
        </span>
      ),
    },
    {
      id: "title",
      label: "Title",
      width: 170,
      sortField: "title",
      filterField: "title",
      editable: "person",
      editField: "job_title",
      copyValue: (r) => r.title,
      render: (r) => (r.title ? <span className="truncate text-fg-2">{r.title}</span> : <Muted />),
    },
    {
      id: "company",
      label: "Company",
      width: 180,
      sortField: "company",
      filterField: "company",
      copyValue: (r) => r.company,
      render: (r) => (r.company ? <span className="truncate text-fg">{r.company}</span> : <Muted />),
    },
    {
      id: "website",
      label: "Website",
      width: 150,
      sortField: "domain",
      filterField: "domain",
      copyValue: (r) => r.website,
      render: (r) =>
        r.website ? (
          <a
            href={r.website}
            target="_blank"
            rel="noreferrer noopener"
            onClick={(e) => e.stopPropagation()}
            className="group/link flex min-w-0 items-center gap-1 text-fg-2 hover:text-accent-strong"
          >
            <span className="truncate">{hostname(r.website)}</span>
            <ExternalLink className="size-3 shrink-0 opacity-0 group-hover/link:opacity-100" />
          </a>
        ) : (
          <Muted />
        ),
    },
    {
      id: "email",
      label: "Email",
      width: 220,
      sortField: "email",
      filterField: "email",
      editable: "person",
      editField: "email",
      copyValue: (r) => r.email,
      render: (r) =>
        r.email ? (
          <span className="flex min-w-0 items-center gap-1">
            <span className="truncate font-mono text-[12px] text-fg">{r.email}</span>
            {r.email_status === "CATCH_ALL" && (
              <Tip content="Catch-all domain: the mail server accepts any address, so this one cannot be confirmed — it follows the most likely name format of the domain.">
                <span className="shrink-0 rounded-[3px] px-1 text-micro font-medium text-warning shadow-[inset_0_0_0_1px_color-mix(in_srgb,var(--warning)_40%,transparent)]">
                  catch-all
                </span>
              </Tip>
            )}
            <CopyButton value={r.email} />
          </span>
        ) : (
          <Muted>No email</Muted>
        ),
    },
    { id: "email_status", label: "Email status", width: 112, sortField: "email_status", filterField: "email_status", render: (r) => <EmailStatusBadge status={r.email_status} /> },
    {
      id: "profile_url",
      label: "Profile",
      width: 92,
      filterField: "profile_url",
      copyValue: (r) => r.profile_url,
      render: (r) =>
        r.profile_url ? (
          <a
            href={r.profile_url}
            target="_blank"
            rel="noreferrer noopener"
            onClick={(e) => e.stopPropagation()}
            className="inline-flex items-center gap-1 text-fg-2 hover:text-accent-strong"
          >
            <span className="rounded-[3px] bg-fg/12 px-1 text-micro font-semibold text-fg-2">in</span>
            <span className="text-meta">Profile</span>
          </a>
        ) : (
          <Muted />
        ),
    },
    {
      id: "location",
      label: "Location",
      width: 130,
      sortField: "city",
      filterField: "city",
      copyValue: (r) => [r.city, r.country].filter(Boolean).join(", "),
      render: (r) => (r.city || r.country ? <span className="truncate text-fg-2">{[r.city, r.country].filter(Boolean).join(", ")}</span> : <Muted />),
    },
    {
      id: "employee_count",
      label: "Size",
      width: 76,
      sortField: "employee_count",
      filterField: "employee_count",
      render: (r) => <span className="tabular text-fg-2">{employees(r.employee_min, r.employee_max)}</span>,
    },
    {
      id: "industry",
      label: "Industry",
      width: 140,
      sortField: "industry",
      filterField: "industry",
      render: (r) => (r.industry ? <span className="truncate text-fg-2">{r.industry}</span> : <Muted />),
    },
    { id: "icp_score", label: "ICP", width: 96, sortField: "icp_score", filterField: "icp_score", render: (r) => <ScoreCell value={r.icp_score} /> },
    {
      id: "overall_confidence",
      label: "Confidence",
      width: 104,
      sortField: "overall_confidence",
      filterField: "overall_confidence",
      render: (r) => <ConfidenceMeter value={r.overall_confidence} />,
    },
    { id: "sources", label: "Sources", width: 150, render: (r) => <SourcesCell sources={r.sources} /> },
    { id: "updated_at", label: "Updated", width: 104, sortField: "updated_at", filterField: "updated_at", render: (r) => <UpdatedCell iso={r.updated_at} /> },
  ];
}

export function companyColumns(): ColumnSpec[] {
  return [
    {
      id: "company",
      label: "Company",
      width: 220,
      minWidth: 140,
      sortField: "company",
      filterField: "company",
      editable: "company",
      editField: "name",
      copyValue: (r) => r.company,
      render: (r) => (
        <span className="flex min-w-0 items-center gap-2">
          <Avatar name={r.company} />
          <span className="truncate font-medium text-fg">{r.company}</span>
          {r.needs_review && <span className="size-1.5 shrink-0 rounded-full bg-warning" title="Needs review" />}
        </span>
      ),
    },
    {
      id: "website",
      label: "Website",
      width: 160,
      sortField: "domain",
      filterField: "domain",
      copyValue: (r) => r.website,
      render: (r) =>
        r.website ? (
          <a href={r.website} target="_blank" rel="noreferrer noopener" onClick={(e) => e.stopPropagation()} className="truncate text-fg-2 hover:text-accent-strong">
            {hostname(r.website)}
          </a>
        ) : (
          <Muted />
        ),
    },
    {
      id: "description",
      label: "Description",
      width: 260,
      filterField: "description",
      render: (r) => (r.description ? <span className="truncate text-fg-2">{r.description}</span> : <Muted />),
    },
    {
      id: "location",
      label: "Location",
      width: 130,
      sortField: "city",
      filterField: "city",
      render: (r) => (r.city || r.country ? <span className="truncate text-fg-2">{[r.city, r.country].filter(Boolean).join(", ")}</span> : <Muted />),
    },
    {
      id: "employee_count",
      label: "Size",
      width: 76,
      sortField: "employee_count",
      filterField: "employee_count",
      render: (r) => <span className="tabular text-fg-2">{employees(r.employee_min, r.employee_max)}</span>,
    },
    {
      id: "industry",
      label: "Industry",
      width: 140,
      sortField: "industry",
      filterField: "industry",
      render: (r) => (r.industry ? <span className="truncate text-fg-2">{r.industry}</span> : <Muted />),
    },
    {
      id: "people_count",
      label: "People",
      width: 76,
      sortField: "people_count",
      filterField: "people_count",
      render: (r) => <span className="tabular text-fg-2">{r.people_count ?? 0}</span>,
    },
    {
      id: "phone",
      label: "Phone",
      width: 140,
      filterField: "phone",
      copyValue: (r) => r.phone,
      render: (r) => (r.phone ? <span className="tabular truncate text-fg-2">{r.phone}</span> : <Muted />),
    },
    {
      id: "company_confidence",
      label: "Confidence",
      width: 104,
      sortField: "company_confidence",
      filterField: "company_confidence",
      render: (r) => <ConfidenceMeter value={r.company_confidence} />,
    },
    { id: "sources", label: "Sources", width: 150, render: (r) => <SourcesCell sources={r.sources} /> },
    { id: "updated_at", label: "Updated", width: 104, sortField: "updated_at", filterField: "updated_at", render: (r) => <UpdatedCell iso={r.updated_at} /> },
  ];
}

export function customColumnSpec(col: ColumnOut): ColumnSpec {
  const key = `cf:${col.id}`;
  return {
    id: key,
    label: col.name,
    width: col.data_type === "boolean" ? 128 : col.data_type === "text" ? 220 : 160,
    sortField: key,
    filterField: key,
    custom: col,
    editable: "cell",
    copyValue: (r) => r.cells[col.id]?.d ?? null,
    render: (r) => <CustomCell cell={r.cells[col.id]} col={col} />,
  };
}
