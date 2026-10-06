"use client";

import {
  Button,
  cn,
  Dialog,
  DialogContent,
  IconButton,
  LiveDot,
  Menu,
  MenuContent,
  MenuItem,
  MenuTrigger,
  NumberTicker,
  ProgressBar,
  ShimmerText,
  Spinner,
  type Tone,
  Tip,
} from "@scout/design-system";
import { useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ChevronRight, CircleCheck, MoreHorizontal, Pause, Pencil, Play, Radar, RotateCcw, Square, WifiOff, X } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import { useLive } from "@/components/shell/live-events";
import { type ApiError } from "@/lib/api";
import { usd } from "@/lib/format";
import { type CampaignAction, campaignAction, qk } from "@/lib/queries";

import { AmendDialog } from "./amend-dialog";
import { ACTIVE, type AmendMode, fmtSilence, type Lang, recovery, type RunView, stageLine, STOPPED, useRunView } from "./run-state";

/* ---- status pill ---------------------------------------------------------------------------------- */

const STATUS_COPY: Record<string, { en: string; fr: string; tone: Tone }> = {
  planning: { en: "Starting", fr: "Démarrage", tone: "info" },
  running: { en: "Running", fr: "En cours", tone: "info" },
  paused: { en: "Paused", fr: "En pause", tone: "warning" },
  completed: { en: "Completed", fr: "Terminée", tone: "success" },
  exhausted: { en: "Out of results", fr: "Épuisée", tone: "warning" },
  budget_reached: { en: "Budget reached", fr: "Budget atteint", tone: "warning" },
  limit_reached: { en: "Limit reached", fr: "Limite atteinte", tone: "warning" },
  failed: { en: "Failed", fr: "Échec", tone: "danger" },
  cancelled: { en: "Cancelled", fr: "Annulée", tone: "neutral" },
  stuck: { en: "Looks stuck", fr: "Bloquée ?", tone: "danger" },
};

const toneText: Record<Tone, string> = {
  neutral: "bg-surface-3 text-fg-2",
  accent: "bg-accent-soft text-accent-strong",
  success: "bg-success-soft text-success",
  warning: "bg-warning-soft text-warning",
  danger: "bg-danger-soft text-danger",
  info: "bg-info-soft text-info",
  muted: "text-fg-3",
};

export function RunStatusPill({ status, stuck, lang = "en" }: { status: string; stuck?: boolean; lang?: Lang }) {
  const key = stuck && ACTIVE.includes(status) ? "stuck" : status;
  const c = STATUS_COPY[key] ?? { en: status.replace(/_/g, " "), fr: status.replace(/_/g, " "), tone: "neutral" as Tone };
  return (
    <span
      key={key}
      className={cn("inline-flex h-[18px] shrink-0 items-center gap-1.5 rounded-xs px-1.5 text-meta font-medium leading-none animate-fade-in transition-colors", toneText[c.tone])}
    >
      {ACTIVE.includes(status) && !stuck ? (
        <LiveDot tone="info" />
      ) : (
        <span className={cn("size-1.5 rounded-full", c.tone === "success" ? "bg-success" : c.tone === "warning" ? "bg-warning" : c.tone === "danger" ? "bg-danger" : "bg-fg-3")} />
      )}
      {c[lang]}
    </span>
  );
}

/* ---- connection ------------------------------------------------------------------------------------ */

export function ConnIndicator({ run, lang = "en", compact }: { run: RunView; lang?: Lang; compact?: boolean }) {
  const fr = lang === "fr";
  if (run.conn === "open" || run.conn === "connecting") {
    if (!ACTIVE.includes(run.status) || compact) return null;
    return (
      <Tip content={fr ? "Mises à jour en direct" : "Live updates"}>
        <span className="hidden items-center gap-1 text-micro font-medium uppercase tracking-wide text-fg-3 sm:inline-flex">
          <LiveDot tone="success" /> Live
        </span>
      </Tip>
    );
  }
  return (
    <span className="inline-flex shrink-0 items-center gap-1 rounded-xs bg-warning-soft px-1.5 py-0.5 text-meta text-warning animate-fade-in" role="status">
      <WifiOff className="size-3" />
      {fr ? "Reconnexion…" : "Reconnecting…"}
    </span>
  );
}

/* ---- actions --------------------------------------------------------------------------------------- */

export function useRunActions(run: RunView | null, lang: Lang = "en") {
  const qc = useQueryClient();
  const [busy, setBusy] = useState<CampaignAction | null>(null);
  const fr = lang === "fr";
  async function act(action: CampaignAction, onBlocked?: (mode: AmendMode) => void) {
    if (!run || busy) return;
    const prev = useLive.getState().campaigns[run.id]?.status ?? run.status;
    setBusy(action);
    if (action === "pause") useLive.getState().patchCampaign(run.id, { status: "paused" }); // instant feedback
    try {
      const st = await campaignAction(run.id, action);
      useLive.getState().patchCampaign(run.id, { status: st.status, reason: st.stop_reason ?? undefined });
      void qc.invalidateQueries({ queryKey: qk.campaign(run.id) });
      void qc.invalidateQueries({ queryKey: ["campaign-live", run.id] });
      void qc.invalidateQueries({ queryKey: qk.campaigns });
      if (action === "pause")
        toast(fr ? "En pause — tout est conservé" : "Paused — everything found is kept", {
          action: { label: fr ? "Reprendre" : "Resume", onClick: () => void act("resume") },
        });
      else if (action === "resume" || action === "retry") toast.success(fr ? "La recherche reprend" : "Search resumed");
      else if (action === "kick") toast(fr ? "Relance envoyée aux workers" : "Workers nudged — retrying now");
      else if (action === "cancel") toast(fr ? "Recherche arrêtée — les leads sont conservés" : "Search cancelled — leads kept");
    } catch (e) {
      useLive.getState().patchCampaign(run.id, { status: prev });
      const err = e as ApiError;
      const mode: AmendMode | null =
        err.code === "target_reached"
          ? "target"
          : err.code === "budget_reached"
            ? "budget"
            : err.code === "exhausted"
              ? "broaden"
              : err.code === "runtime_limit"
                ? "runtime"
                : null;
      toast.error(err.message, {
        description: err.hint,
        action: mode && onBlocked ? { label: fr ? "Modifier…" : "Change…", onClick: () => onBlocked(mode) } : undefined,
      });
    } finally {
      setBusy(null);
    }
  }
  return { act, busy };
}

export function RunActions({ run, lang = "en", size = "xs", showOpen = false }: { run: RunView; lang?: Lang; size?: "xs" | "sm"; showOpen?: boolean }) {
  const fr = lang === "fr";
  const { act, busy } = useRunActions(run, lang);
  const router = useRouter();
  const [amend, setAmend] = useState<AmendMode | null>(null);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const active = ACTIVE.includes(run.status);
  const rec = recovery(run.status, lang);
  const spin = (a: CampaignAction, icon: React.ReactNode) => (busy === a ? <Spinner size={11} /> : icon);
  return (
    <span className="flex shrink-0 items-center gap-0.5">
      {active && run.stall === "stuck" && (
        <Button size={size} variant="primary" className="press" disabled={!!busy} onClick={() => void act("kick")}>
          {spin("kick", <RotateCcw />)} {fr ? "Relancer" : "Retry"}
        </Button>
      )}
      {active && (
        <Button size={size} variant="ghost" className="press" disabled={!!busy} onClick={() => void act("pause")} aria-label={fr ? "Mettre en pause" : "Pause"}>
          {spin("pause", <Pause />)} <span className="hidden sm:inline">{fr ? "Pause" : "Pause"}</span>
        </Button>
      )}
      {run.status === "paused" && (
        <Button size={size} variant="primary" className="press" disabled={!!busy} onClick={() => void act("resume", setAmend)}>
          {spin("resume", <Play />)} {fr ? "Reprendre" : "Resume"}
        </Button>
      )}
      {rec && (
        <Button size={size} variant={run.status === "paused" ? "ghost" : "primary"} className="press" disabled={!!busy} onClick={() => setAmend(rec.mode)}>
          <Pencil /> <span className={cn(run.status === "paused" && "hidden md:inline")}>{rec.label}</span>
        </Button>
      )}
      {run.status === "failed" && (
        <Button size={size} variant="primary" className="press" disabled={!!busy} onClick={() => void act("retry")}>
          {spin("retry", <RotateCcw />)} {fr ? "Réessayer" : "Retry"}
        </Button>
      )}
      {showOpen && run.listId && (
        <Link href={`/lists/${run.listId}`} className="press rounded-sm px-1.5 py-0.5 text-meta text-accent-strong hover:bg-surface-2">
          {fr ? "Voir la liste" : "Open list"}
        </Link>
      )}
      {(active || run.status === "paused") && (
        <Menu>
          <MenuTrigger asChild>
            <IconButton label={fr ? "Plus d'actions" : "More actions"} size="xs">
              <MoreHorizontal className="size-3.5" />
            </IconButton>
          </MenuTrigger>
          <MenuContent align="end">
            {active && (
              <MenuItem icon={<Pencil />} onSelect={() => setAmend("adjust")}>
                {fr ? "Modifier la recherche…" : "Change the search…"}
              </MenuItem>
            )}
            <MenuItem icon={<Radar />} onSelect={() => router.push(`/campaigns/${run.id}`)}>
              {fr ? "Détails de la campagne" : "Campaign details"}
            </MenuItem>
            <MenuItem icon={<Square />} danger onSelect={() => setConfirmCancel(true)}>
              {fr ? "Arrêter définitivement…" : "Cancel for good…"}
            </MenuItem>
          </MenuContent>
        </Menu>
      )}
      {amend && <AmendDialog campaignId={run.id} open={Boolean(amend)} onOpenChange={(v) => !v && setAmend(null)} mode={amend} lang={lang} status={run.status} />}
      <Dialog open={confirmCancel} onOpenChange={setConfirmCancel}>
        <DialogContent
          title={fr ? "Arrêter cette recherche définitivement ?" : "Cancel this search for good?"}
          description={
            fr
              ? "Les leads déjà trouvés restent dans ta liste. Mets plutôt en pause si tu veux reprendre plus tard."
              : "Leads already found stay in your list. Pause instead if you may want to continue later."
          }
          width={440}
        >
          <div className="flex justify-end gap-2 px-4 py-3">
            <Button
              variant="ghost"
              onClick={() => {
                setConfirmCancel(false);
                void act("pause");
              }}
            >
              <Pause /> {fr ? "Mettre en pause" : "Pause instead"}
            </Button>
            <Button
              variant="danger"
              onClick={() => {
                setConfirmCancel(false);
                void act("cancel");
              }}
            >
              <X /> {fr ? "Arrêter" : "Cancel search"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </span>
  );
}

/* ---- funnel ---------------------------------------------------------------------------------------- */

export function Funnel({ run, lang = "en", className }: { run: RunView; lang?: Lang; className?: string }) {
  const fr = lang === "fr";
  const steps: [string, number][] = [
    [fr ? "Trouvées" : "Discovered", run.raw],
    [fr ? "Analysées" : "Analysed", run.evaluated],
    [fr ? "Dirigeants" : "Decision-makers", run.people],
    [fr ? "Emails" : "Emails", run.emails],
  ];
  return (
    <div className={cn("flex min-w-0 items-center gap-1.5 overflow-x-auto whitespace-nowrap text-meta scroll-quiet", className)}>
      {steps.map(([label, v]) => (
        <span key={label} className="flex shrink-0 items-center gap-1.5">
          <span className="text-fg-3">{label}</span>
          <NumberTicker value={v} className="font-medium text-fg-2" />
          <ChevronRight className="size-3 text-fg-3/60" />
        </span>
      ))}
      <span className="flex shrink-0 items-center gap-1.5">
        <span className="text-fg-3">{fr ? "Qualifiés" : "Qualified"}</span>
        <span className="font-medium text-fg">
          <NumberTicker value={run.qualified} />
          <span className="text-fg-3"> / {run.target.toLocaleString()}</span>
        </span>
      </span>
    </div>
  );
}

/* ---- stall banner ---------------------------------------------------------------------------------- */

export function StallNotice({ run, lang = "en" }: { run: RunView; lang?: Lang }) {
  const fr = lang === "fr";
  if (!ACTIVE.includes(run.status) || run.stall === "none") return null;
  const h = run.health;
  const known: string[] = [];
  if (run.silenceS !== null) known.push(fr ? `aucune activité depuis ${fmtSilence(run.silenceS, lang)}` : `no activity for ${fmtSilence(run.silenceS, lang)}`);
  if (h?.overdue_jobs) known.push(fr ? `${h.overdue_jobs} tâches attendent un worker` : `${h.overdue_jobs} jobs waiting for a worker`);
  if (h && !h.workers_enabled) known.push(fr ? "les workers sont désactivés sur ce serveur" : "workers are disabled on this server");
  if (run.conn !== "open") known.push(fr ? "flux temps réel déconnecté" : "live stream disconnected");
  if (run.stall === "slow") {
    return (
      <p className="flex items-center gap-1.5 text-meta text-fg-3 animate-fade-in">
        <Spinner size={10} />
        {fr ? "Toujours en recherche — les sources sont lentes, aucun nouveau résultat depuis 2 min." : "Still searching — sources are slow, no new result for 2 min."}
      </p>
    );
  }
  return (
    <div role="alert" className="flex flex-wrap items-start gap-x-2 gap-y-0.5 rounded-sm bg-danger-soft px-2 py-1.5 text-meta animate-fade-in">
      <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-danger" />
      <span className="font-medium text-danger">{fr ? "Ça semble bloqué" : "Looks stuck"}</span>
      <span className="text-fg-2">{known.join(" · ")}</span>
      {h?.last_error && (
        <span className="w-full truncate pl-5 text-fg-3" title={h.last_error}>
          {fr ? "Dernière erreur : " : "Last error: "}
          {h.last_error}
        </span>
      )}
    </div>
  );
}

/* ---- the header ------------------------------------------------------------------------------------ */

export function RunHeader({ campaignId, onDismiss }: { campaignId: string; onDismiss?: () => void }) {
  const run = useRunView(campaignId);
  if (!run || !run.loaded) {
    return (
      <div className="flex h-[58px] shrink-0 items-center gap-3 border-b border-line px-3">
        <span className="skeleton-shimmer h-3 w-16 rounded-xs" />
        <span className="skeleton-shimmer h-3 w-48 rounded-xs" />
      </div>
    );
  }
  const stage = stageLine(run, "en");
  const active = ACTIVE.includes(run.status);
  const stopped = STOPPED.includes(run.status);
  const pct = run.target ? Math.min(100, Math.round((run.qualified / run.target) * 100)) : 0;
  return (
    <section aria-label={`Search ${run.name}`} className="shrink-0 border-b border-line bg-bg/40 px-3 py-2 animate-fade-in">
      <div className="flex min-w-0 items-center gap-2">
        <RunStatusPill status={run.status} stuck={run.stall === "stuck"} />
        <Link href={`/campaigns/${run.id}`} className="hidden min-w-0 max-w-[220px] truncate text-body font-medium text-fg hover:underline sm:block">
          {run.name}
        </Link>
        <span className="hidden text-fg-3 sm:inline">·</span>
        <span className="min-w-0 flex-1 truncate text-body" aria-live="polite">
          {stage.active ? (
            <ShimmerText>{stage.text}</ShimmerText>
          ) : (
            <span className={cn(stage.tone === "success" ? "text-success" : stage.tone === "danger" ? "text-danger" : stage.tone === "warning" ? "text-warning" : "text-fg-2")}>
              {stage.tone === "success" && <CircleCheck className="mr-1 inline size-3.5 align-[-2px]" />}
              {stage.text}
            </span>
          )}
        </span>
        <ConnIndicator run={run} />
        <RunActions run={run} />
        {stopped && onDismiss && (
          <IconButton label="Hide" size="xs" onClick={onDismiss}>
            <X className="size-3.5" />
          </IconButton>
        )}
      </div>
      <div className="mt-1.5 flex min-w-0 items-center gap-3">
        <Funnel run={run} className="min-w-0 flex-1" />
        <ProgressBar value={run.qualified} max={Math.max(1, run.target)} className="hidden w-24 shrink-0 md:block" tone={run.status === "completed" ? "success" : "accent"} />
        <span className="hidden shrink-0 items-center gap-2 text-meta text-fg-3 tabular lg:flex">
          <span>{pct}%</span>
          {active && <span>{run.rate ? `${run.rate}/min` : "measuring…"}</span>}
          {active && run.eta ? <span>~{run.eta} min left</span> : null}
          <Tip content={run.maxCost ? `Budget ${usd(run.maxCost)}` : "No campaign budget"}>
            <span>{usd(run.cost)}</span>
          </Tip>
        </span>
      </div>
      {(run.stall !== "none" || (stopped && run.reason && run.status !== "completed")) && (
        <div className="mt-1.5">
          {run.stall !== "none" ? (
            <StallNotice run={run} />
          ) : (
            <p className="text-meta text-fg-3">
              {run.reason}
              {run.status === "exhausted" && " Broadening the criteria (another city, a wider size, more roles) usually finds more."}
            </p>
          )}
        </div>
      )}
    </section>
  );
}
