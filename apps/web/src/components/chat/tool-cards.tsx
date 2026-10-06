"use client";

import { Badge, Button, cn, ProgressBar, ShimmerText, Spinner } from "@scout/design-system";
import { useQueryClient } from "@tanstack/react-query";
import { AlertCircle, Check, Columns3, Download, ListChecks, Pencil, Play, Radar, Undo2 } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { toast } from "sonner";

import { ConnIndicator, Funnel, RunActions, RunStatusPill, StallNotice } from "@/components/campaign/run-header";
import { stageLine, STOPPED, useRunView } from "@/components/campaign/run-state";
import { useLive } from "@/components/shell/live-events";
import { api, download } from "@/lib/api";
import { n } from "@/lib/format";
import { qk } from "@/lib/queries";
import { useUI } from "@/lib/store";

type Card = Record<string, unknown> & { kind: string };

function Shell({
  icon,
  title,
  children,
  tone = "neutral",
  actions,
}: {
  icon: React.ReactNode;
  title: React.ReactNode;
  children?: React.ReactNode;
  tone?: "neutral" | "success" | "danger";
  actions?: React.ReactNode;
}) {
  return (
    <div className="animate-fade-in rounded-md bg-surface-1 shadow-[inset_0_0_0_1px_var(--border-subtle)]">
      <div className="flex items-center gap-2 px-2.5 py-2">
        <span
          className={cn(
            "grid size-5 shrink-0 place-items-center rounded-xs [&_svg]:size-3.5",
            tone === "success" ? "bg-success-soft text-success" : tone === "danger" ? "bg-danger-soft text-danger" : "bg-surface-3 text-fg-2",
          )}
        >
          {icon}
        </span>
        <div className="min-w-0 flex-1 truncate text-body font-medium text-fg">{title}</div>
        {actions}
      </div>
      {children && <div className="px-2.5 pb-2.5">{children}</div>}
    </div>
  );
}

function UndoButton({ auditId }: { auditId?: unknown }) {
  const qc = useQueryClient();
  const [done, setDone] = useState(false);
  if (!auditId) return null;
  return (
    <Button
      size="xs"
      variant="ghost"
      disabled={done}
      onClick={async () => {
        try {
          await api(`activity/${auditId}/undo`, { method: "POST" });
          setDone(true);
          toast("Undone");
          qc.invalidateQueries({ queryKey: ["rows"] });
          qc.invalidateQueries({ queryKey: qk.lists });
          qc.invalidateQueries({ queryKey: ["columns"] });
        } catch (e) {
          toast.error((e as Error).message);
        }
      }}
    >
      <Undo2 /> {done ? "Undone" : "Undo"}
    </Button>
  );
}

export function ToolCard({ card, status, compact }: { card: Card; status?: string; compact?: boolean }) {
  const failed = status === "failed" || card.kind === "error";
  if (failed) {
    return (
      <Shell icon={<AlertCircle />} title={String(card.title ?? "Action failed")} tone="danger">
        <p className="text-meta text-fg-2">{String(card.detail ?? "")}</p>
        {card.hint ? <p className="mt-1 text-meta text-fg-3">{String(card.hint)}</p> : null}
      </Shell>
    );
  }
  switch (card.kind) {
    case "list_created":
      return (
        <Shell
          icon={<Check />}
          tone="success"
          title={
            <>
              {String(card.title)} <span className="font-normal text-fg-2">{String(card.name)}</span>
            </>
          }
          actions={
            <>
              <Link href={`/lists/${card.list_id}`} className="rounded-sm px-1.5 py-0.5 text-meta text-accent-strong hover:bg-surface-2">
                View
              </Link>
              <UndoButton auditId={card.audit_id} />
            </>
          }
        >
          {Number(card.added ?? 0) > 0 && <p className="text-meta text-fg-3">{n(Number(card.added))} leads added</p>}
        </Shell>
      );
    case "rows_affected":
    case "filter_applied":
      return (
        <Shell icon={card.kind === "filter_applied" ? <ListChecks /> : <Check />} tone="success" title={String(card.title)} actions={<UndoButton auditId={card.audit_id} />}>
          {card.detail ? <p className="text-meta text-fg-2">{String(card.detail)}</p> : null}
        </Shell>
      );
    case "campaign_started":
    case "campaign_progress":
      return compact ? <CampaignLine card={card} /> : <CampaignCard card={card} />;
    case "campaign_amended":
      return <AmendedCard card={card} />;
    case "column_created":
      return <ColumnCard card={card} />;
    case "enrichment_progress":
      return <EnrichmentCard card={card} />;
    case "export_ready":
      return (
        <Shell
          icon={<Download />}
          tone="success"
          title="Export ready"
          actions={
            <Button
              size="xs"
              variant="ghost"
              onClick={async () => {
                try {
                  const r = await download("exports", card.request);
                  toast.success(`Exported ${r.rows.toLocaleString()} rows`);
                } catch (e) {
                  toast.error((e as Error).message);
                }
              }}
            >
              <Download /> Download again
            </Button>
          }
        />
      );
    case "lead_summary":
      return (
        <Shell icon={<Radar />} title={String(card.title)} actions={card.entity_id ? <OpenLead id={String(card.entity_id)} /> : null}>
          <ul className="space-y-0.5">
            {((card.bullets as string[]) ?? []).map((b) => (
              <li key={b} className="flex gap-1.5 text-meta text-fg-2">
                <Check className="mt-0.5 size-3 shrink-0 text-success" />
                {b}
              </li>
            ))}
          </ul>
        </Shell>
      );
    case "sources":
    case "history":
      return (
        <Shell
          icon={<ListChecks />}
          title={String(card.title)}
          actions={card.entity_id ? <OpenLead id={String(card.entity_id)} entityType={String(card.entity_type ?? "person")} /> : null}
        />
      );
    default:
      return (
        <Shell icon={<Check />} title={String(card.title ?? card.kind)}>
          {card.detail ? <p className="text-meta text-fg-2">{String(card.detail)}</p> : null}
        </Shell>
      );
  }
}

function OpenLead({ id, entityType = "person" }: { id: string; entityType?: string }) {
  const openDrawer = useUI((s) => s.openDrawer);
  return (
    <button
      type="button"
      className="rounded-sm px-1.5 py-0.5 text-meta text-accent-strong hover:bg-surface-2"
      onClick={() => openDrawer(entityType === "company" ? "company" : "person", id)}
    >
      Open
    </button>
  );
}

const LABELS_FR: Record<string, string> = {
  Target: "Objectif",
  Companies: "Entreprises",
  Location: "Lieu",
  Size: "Taille",
  People: "Contacts",
  Email: "Email",
  "Previously seen": "Déjà vus",
  Website: "Site web",
  Technology: "Techno",
  Column: "Colonne",
};

function Criteria({ items, lang }: { items: { label: string; value: string }[]; lang: "fr" | "en" }) {
  return (
    <dl className="grid grid-cols-[92px_1fr] gap-x-2 gap-y-0.5 text-meta">
      {items.map((i) => (
        <div key={i.label + i.value} className="contents">
          <dt className="text-fg-3">{lang === "fr" ? (LABELS_FR[i.label] ?? i.label) : i.label}</dt>
          <dd className="min-w-0 truncate text-fg-2" title={i.value}>
            {i.value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

/** "Here is what I'll search" — nothing runs until Launch. Launch is idempotent server-side. */
export function PlanCard({ card, onLaunch, onEdit }: { card: Card; onLaunch?: (card: Card) => Promise<void>; onEdit?: (text: string) => void }) {
  const lang = card.lang === "fr" ? "fr" : "en";
  const fr = lang === "fr";
  const [busy, setBusy] = useState(false);
  const interp = (card.interpretation as { label: string; value: string }[]) ?? [];
  const sources = (card.sources as string[]) ?? [];
  const warnings = (card.warnings as string[]) ?? [];
  const launched = Boolean(card.launched_campaign_id);
  const superseded = Boolean(card.superseded);
  if (launched || superseded) {
    return (
      <div className="flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-meta text-fg-3 shadow-[inset_0_0_0_1px_var(--border-subtle)] animate-fade-in">
        {launched ? <Check className="size-3.5 text-success" /> : <Pencil className="size-3.5" />}
        <span className="truncate">
          {launched ? (fr ? "Plan lancé" : "Plan launched") : fr ? "Plan remplacé" : "Plan replaced"} · {String(card.name ?? "")}
        </span>
      </div>
    );
  }
  return (
    <div className="lift rounded-md bg-surface-1 shadow-[inset_0_0_0_1px_var(--border-strong)] animate-fade-in">
      <div className="flex items-center gap-2 px-3 pt-2.5">
        <span className="grid size-5 place-items-center rounded-xs bg-accent-soft text-accent [&_svg]:size-3.5">
          <Radar />
        </span>
        <div className="min-w-0">
          <div className="text-micro font-medium uppercase tracking-wide text-fg-3">{fr ? "Plan de recherche" : "Search plan"}</div>
          <div className="truncate text-body font-medium text-fg">{String(card.name ?? "")}</div>
        </div>
      </div>
      <div className="space-y-2 px-3 py-2.5">
        <Criteria items={interp} lang={lang} />
        {sources.length > 0 && (
          <p className="text-meta text-fg-3">
            {fr ? "Sources : " : "Sources: "}
            <span className="text-fg-2">{sources.join(" · ")}</span>
          </p>
        )}
        {warnings.map((w) => (
          <p key={w} className="flex gap-1.5 text-meta text-warning">
            <AlertCircle className="mt-0.5 size-3 shrink-0" />
            {w}
          </p>
        ))}
      </div>
      <div className="flex items-center gap-1.5 border-t border-line px-3 py-2">
        <Button size="xs" variant="ghost" className="press" disabled={busy} onClick={() => onEdit?.(String(card.original_request ?? card.request ?? ""))}>
          <Pencil /> {fr ? "Modifier" : "Edit"}
        </Button>
        <span className="flex-1" />
        <Button
          size="xs"
          variant="primary"
          className="press"
          disabled={busy || !onLaunch}
          onClick={async () => {
            if (busy || !onLaunch) return;
            setBusy(true);
            try {
              await onLaunch(card);
            } finally {
              setBusy(false);
            }
          }}
        >
          {busy ? <Spinner size={11} className="text-accent-contrast" /> : <Play />}
          {fr ? "Lancer la recherche" : "Launch search"}
        </Button>
      </div>
    </div>
  );
}

/** The live run, right in the conversation: current stage (shimmer), counts, progress, pause / resume /
 * resume with changes, stall + stop explanations. */
function CampaignCard({ card }: { card: Card }) {
  const id = String(card.campaign_id ?? "");
  const lang = card.lang === "fr" ? "fr" : "en";
  const fr = lang === "fr";
  const run = useRunView(id || null);
  const [showCriteria, setShowCriteria] = useState(false);
  if (!run || !run.loaded) {
    return (
      <Shell icon={<Radar />} title={String(card.title ?? "Campaign")}>
        <span className="block h-2.5 w-2/3 rounded-xs skeleton-shimmer" />
      </Shell>
    );
  }
  const stage = stageLine(run, lang);
  const interp = (card.interpretation as { label: string; value: string }[] | undefined) ?? run.interpretation;
  const terminal = STOPPED.includes(run.status);
  return (
    <div className="rounded-md bg-surface-1 shadow-[inset_0_0_0_1px_var(--border-subtle)] animate-fade-in">
      <div className="flex items-center gap-2 px-2.5 pt-2">
        <span className="grid size-5 shrink-0 place-items-center rounded-xs bg-surface-3 text-fg-2 [&_svg]:size-3.5">
          <Radar />
        </span>
        <div className="min-w-0 flex-1 truncate text-body font-medium text-fg">{run.name}</div>
        <RunStatusPill status={run.status} stuck={run.stall === "stuck"} lang={lang} />
      </div>
      <div className="space-y-1.5 px-2.5 pb-2.5 pt-1.5">
        {card.event ? <p className="text-meta text-fg-3">{eventLabel(String(card.event), lang)}</p> : null}
        <p className="truncate text-body" aria-live="polite">
          {stage.active ? (
            <ShimmerText>{stage.text}</ShimmerText>
          ) : (
            <span className={cn(stage.tone === "success" ? "text-success" : stage.tone === "danger" ? "text-danger" : stage.tone === "warning" ? "text-warning" : "text-fg-2")}>
              {stage.text}
            </span>
          )}
        </p>
        <Funnel run={run} lang={lang} wrap />
        <ProgressBar value={run.qualified} max={Math.max(1, run.target)} tone={run.status === "completed" ? "success" : "accent"} />
        <StallNotice run={run} lang={lang} />
        {terminal && run.reason && run.status !== "completed" && <p className="text-meta text-fg-3">{run.reason}</p>}
        {interp.length > 0 && (
          <div>
            <button type="button" onClick={() => setShowCriteria((v) => !v)} className="text-meta text-fg-3 hover:text-fg-2" aria-expanded={showCriteria}>
              {showCriteria ? (fr ? "Masquer les critères" : "Hide criteria") : fr ? "Voir les critères" : "Show criteria"}
            </button>
            {showCriteria && (
              <div className="mt-1 animate-fade-in">
                <Criteria items={interp} lang={lang} />
              </div>
            )}
          </div>
        )}
        <div className="flex flex-wrap items-center justify-between gap-1 pt-0.5">
          <ConnIndicator run={run} lang={lang} compact />
          <span className="flex-1" />
          <RunActions run={run} lang={lang} showOpen />
        </div>
      </div>
    </div>
  );
}

/** An earlier card of a run that has a newer card further down: one quiet line, the live card stays below. */
function CampaignLine({ card }: { card: Card }) {
  const id = String(card.campaign_id ?? "");
  const lang = card.lang === "fr" ? "fr" : "en";
  const run = useRunView(id || null);
  return (
    <div className="flex items-center gap-2 rounded-md px-2.5 py-1.5 text-meta text-fg-3 shadow-[inset_0_0_0_1px_var(--border-subtle)]">
      <Radar className="size-3.5 shrink-0" />
      <span className="min-w-0 flex-1 truncate">
        {card.event ? `${eventLabel(String(card.event), lang)} · ` : ""}
        {run?.name ?? String(card.title ?? "")}
      </span>
      {run?.loaded && (
        <span className="tabular shrink-0">
          {run.qualified.toLocaleString()} / {run.target.toLocaleString()}
        </span>
      )}
    </div>
  );
}

function eventLabel(e: string, lang: "fr" | "en"): string {
  const fr: Record<string, string> = { Paused: "Mise en pause", Resumed: "Reprise", Cancelled: "Arrêtée" };
  return lang === "fr" ? (fr[e] ?? e) : e;
}

function AmendedCard({ card }: { card: Card }) {
  const lang = card.lang === "fr" ? "fr" : "en";
  const fr = lang === "fr";
  const changes = (card.changes as { field: string; label: string; before: string; after: string }[]) ?? [];
  const blocked = card.resume_blocked as { message: string; hint?: string } | null;
  return (
    <Shell icon={<Check />} tone={blocked ? "neutral" : "success"} title={(fr ? "Recherche modifiée · " : "Search updated · ") + String(card.title ?? "")}>
      <ul className="space-y-0.5">
        {changes.map((c) => (
          <li key={c.field} className="grid grid-cols-[84px_1fr] gap-2 text-meta">
            <span className="text-fg-3">{lang === "fr" ? (LABELS_FR[c.label] ?? c.label) : c.label}</span>
            <span className="min-w-0 truncate">
              <span className="text-fg-3 line-through decoration-fg-3/50">{c.before}</span> <span className="text-fg-3">→</span>{" "}
              <span className="font-medium text-accent-strong">{c.after}</span>
            </span>
          </li>
        ))}
      </ul>
      <p className={cn("mt-1.5 text-meta", blocked ? "text-warning" : "text-fg-3")}>
        {blocked
          ? `${blocked.message}${blocked.hint ? ` — ${blocked.hint}` : ""}`
          : card.resumed
            ? fr
              ? "La recherche a repris."
              : "The search resumed."
            : fr
              ? "Modifications enregistrées."
              : "Changes saved."}
      </p>
    </Shell>
  );
}

export function StatusText({ status }: { status: string }) {
  const tone =
    status === "completed"
      ? "success"
      : ["running", "planning"].includes(status)
        ? "info"
        : status === "paused"
          ? "warning"
          : ["failed", "cancelled"].includes(status)
            ? "danger"
            : "neutral";
  return (
    <Badge tone={tone} dot>
      {status.replace(/_/g, " ")}
    </Badge>
  );
}

function ColumnCard({ card }: { card: Card }) {
  const id = String(card.column_id ?? "");
  const progress = useLive((s) => s.columnProgress[id]);
  const describe = (card.describe as Record<string, string>) ?? {};
  const coverage = (card.coverage as { total?: number; cached?: number }) ?? {};
  const total = progress?.total ?? Number(card.queued ?? coverage.total ?? 0);
  const done = progress?.done ?? 0;
  return (
    <Shell icon={<Columns3 />} tone="success" title={String(card.title)} actions={<UndoButton auditId={card.audit_id} />}>
      <dl className="grid grid-cols-[88px_1fr] gap-x-2 gap-y-0.5 text-meta">
        <dt className="text-fg-3">Resolver</dt>
        <dd className="text-fg-2">{describe.resolver_label ?? "—"}</dd>
        <dt className="text-fg-3">Sources</dt>
        <dd className="text-fg-2">{describe.sources_label ?? "—"}</dd>
        {coverage.total !== undefined && (
          <>
            <dt className="text-fg-3">Coverage</dt>
            <dd className="text-fg-2">
              {n(coverage.cached ?? 0)} / {n(coverage.total)} already cached
            </dd>
          </>
        )}
      </dl>
      {total > 0 && (
        <div className="mt-2">
          <div className="flex justify-between text-meta text-fg-3">
            <span>{done >= total ? "Enriched" : "Enriching"}</span>
            <span className="tabular">
              {n(done)} / {n(total)}
            </span>
          </div>
          <ProgressBar value={done} max={total} className="mt-1" tone={done >= total ? "success" : "accent"} />
        </div>
      )}
    </Shell>
  );
}

function EnrichmentCard({ card }: { card: Card }) {
  const id = String(card.column_id ?? "");
  const progress = useLive((s) => (id ? s.columnProgress[id] : undefined));
  const total = progress?.total ?? Number(card.queued ?? 0);
  const done = progress?.done ?? 0;
  return (
    <Shell icon={<Columns3 />} title={String(card.title)}>
      <div className="flex justify-between text-meta text-fg-3">
        <span>{total === 0 ? "Nothing to do — already up to date" : done >= total ? "Done" : "Running"}</span>
        {total > 0 && (
          <span className="tabular">
            {n(done)} / {n(total)}
          </span>
        )}
      </div>
      {total > 0 && <ProgressBar value={done} max={total} className="mt-1" />}
    </Shell>
  );
}
