"use client";

import { Badge, Button, cn, ProgressBar } from "@scout/design-system";
import { useQueryClient } from "@tanstack/react-query";
import { AlertCircle, Check, Columns3, Download, ListChecks, Pause, Play, Radar, Undo2, X } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { toast } from "sonner";

import { useLive } from "@/components/shell/live-events";
import { api, download } from "@/lib/api";
import { n } from "@/lib/format";
import { qk, useCampaign } from "@/lib/queries";
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

export function ToolCard({ card, status }: { card: Card; status?: string }) {
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
      return <CampaignCard card={card} />;
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

const ACTIVE = ["running", "planning"];

function CampaignCard({ card }: { card: Card }) {
  const id = String(card.campaign_id ?? "");
  const q = useCampaign(id || null);
  const live = useLive((s) => s.campaigns[id]);
  const qc = useQueryClient();
  const data = q.data;
  const status = live?.status ?? data?.status ?? "planning";
  const qualified = live?.qualified ?? data?.stats?.qualified ?? 0;
  const target = Number(card.target ?? data?.target ?? 0);
  const interp = (card.interpretation as { label: string; value: string }[] | undefined) ?? data?.interpretation ?? [];
  async function act(a: "pause" | "resume" | "cancel") {
    try {
      await api(`campaigns/${id}/${a}`, { method: "POST" });
      qc.invalidateQueries({ queryKey: qk.campaign(id) });
    } catch (e) {
      toast.error((e as Error).message);
    }
  }
  return (
    <Shell
      icon={<Radar />}
      title={String(card.title ?? data?.name ?? "Campaign")}
      actions={
        <span className="flex items-center gap-0.5">
          {ACTIVE.includes(status) && (
            <Button size="xs" variant="ghost" onClick={() => act("pause")}>
              <Pause /> Pause
            </Button>
          )}
          {status === "paused" && (
            <Button size="xs" variant="ghost" onClick={() => act("resume")}>
              <Play /> Resume
            </Button>
          )}
          {(ACTIVE.includes(status) || status === "paused") && (
            <Button size="xs" variant="ghost" onClick={() => act("cancel")}>
              <X /> Cancel
            </Button>
          )}
          <Link href={`/campaigns/${id}`} className="rounded-sm px-1.5 py-0.5 text-meta text-accent-strong hover:bg-surface-2">
            View
          </Link>
        </span>
      }
    >
      {interp.length > 0 && card.kind === "campaign_started" && (
        <dl className="mb-2 grid grid-cols-[88px_1fr] gap-x-2 gap-y-0.5 text-meta">
          {interp.map((i) => (
            <div key={i.label + i.value} className="contents">
              <dt className="text-fg-3">{i.label}</dt>
              <dd className="truncate text-fg-2" title={i.value}>
                {i.value}
              </dd>
            </div>
          ))}
        </dl>
      )}
      <div className="flex items-baseline justify-between text-meta">
        <span className="text-fg-2">
          <span className="tabular font-medium text-fg">{n(qualified)}</span> / {n(target)} qualified
        </span>
        <StatusText status={status} />
      </div>
      <ProgressBar value={qualified} max={Math.max(1, target)} className="mt-1.5" tone={status === "completed" ? "success" : "accent"} />
      {(data?.stop_reason || live?.reason) && !ACTIVE.includes(status) && <p className="mt-1.5 text-meta text-fg-3">{live?.reason ?? data?.stop_reason}</p>}
      {ACTIVE.includes(status) && (
        <p className="mt-1.5 text-meta text-fg-3">
          {n(live?.raw ?? data?.stats?.raw_discovered ?? 0)} discovered · {n(live?.evaluated ?? data?.stats?.companies_evaluated ?? 0)} evaluated
          {live?.eta_minutes ? ` · ~${live.eta_minutes} min left` : ""}
        </p>
      )}
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
