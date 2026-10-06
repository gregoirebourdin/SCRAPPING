"use client";

import { Button, cn, Dialog, DialogContent, Kbd, Spinner, Textarea } from "@scout/design-system";
import { useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowRight, Check, Play } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { api, ApiError } from "@/lib/api";
import { type AmendBody, type AmendResult, amendCampaign, qk } from "@/lib/queries";

import type { AmendMode, Lang } from "./run-state";

/* "Resume with changes…": describe the change in your own words (or tap a suggestion), see exactly what will
   change as a diff, then apply — the search resumes where it stopped and keeps every lead already found. */

const COPY = {
  en: {
    title: {
      resume: "Resume with changes",
      broaden: "Broaden the search",
      budget: "Raise the budget",
      target: "Find more leads",
      runtime: "Extend the search",
      adjust: "Change the search",
    },
    desc: "Leads already found stay in your list and are never repeated. Discovery continues where it stopped.",
    placeholder: "e.g. add Marseille too · founders only · +100 leads · 2–50 employees · budget $10",
    preview: "What will change",
    nothing: "Describe a change to preview it.",
    pauseNote: "The search pauses for a moment, applies the change, then continues.",
    cancel: "Cancel",
    apply: "Apply",
    applyResume: "Apply & resume",
    done: "Search updated",
    blocked: "Updated, but it can't resume yet",
    undo: "Undo",
    undone: "Previous criteria restored",
  },
  fr: {
    title: {
      resume: "Reprendre avec des modifs",
      broaden: "Élargir la recherche",
      budget: "Augmenter le budget",
      target: "Trouver plus de leads",
      runtime: "Prolonger la recherche",
      adjust: "Modifier la recherche",
    },
    desc: "Les leads déjà trouvés restent dans ta liste et ne reviendront jamais. La découverte reprend là où elle s'est arrêtée.",
    placeholder: "ex. ajoute aussi Marseille · seulement les fondateurs · +100 leads · 2–50 salariés · budget 10 $",
    preview: "Ce qui va changer",
    nothing: "Décris une modification pour la prévisualiser.",
    pauseNote: "La recherche se met en pause un instant, applique la modification, puis continue.",
    cancel: "Annuler",
    apply: "Appliquer",
    applyResume: "Appliquer et reprendre",
    done: "Recherche mise à jour",
    blocked: "Modifiée, mais elle ne peut pas encore reprendre",
    undo: "Annuler",
    undone: "Critères précédents restaurés",
  },
};

const SUGGESTIONS: Record<Lang, Record<AmendMode, string[]>> = {
  en: {
    resume: ["+50 leads", "add Paris too", "founders only", "2–50 employees"],
    broaden: ["add Paris too", "add Marseille too", "any company size", "add the CMOs too"],
    budget: ["budget $5", "budget $10", "budget $25"],
    target: ["+25 leads", "+50 leads", "+100 leads"],
    runtime: ["+24h runtime"],
    adjust: ["+50 leads", "add Paris too", "founders only", "budget $10"],
  },
  fr: {
    resume: ["+50 leads", "ajoute aussi Paris", "seulement les fondateurs", "2–50 salariés"],
    broaden: ["ajoute aussi Paris", "ajoute aussi Marseille", "toute taille", "ajoute aussi les CMO"],
    budget: ["budget 5 $", "budget 10 $", "budget 25 $"],
    target: ["+25 leads", "+50 leads", "+100 leads"],
    runtime: ["+24h"],
    adjust: ["+50 leads", "ajoute aussi Paris", "seulement les fondateurs", "budget 10 $"],
  },
};

export function AmendDialog({
  campaignId,
  open,
  onOpenChange,
  mode = "resume",
  lang = "en",
  status,
  runtimeHours,
  onApplied,
}: {
  campaignId: string;
  open: boolean;
  onOpenChange: (v: boolean) => void;
  mode?: AmendMode;
  lang?: Lang;
  status?: string;
  runtimeHours?: number;
  onApplied?: (r: AmendResult) => void;
}) {
  const t = COPY[lang];
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const [preview, setPreview] = useState<AmendResult | null>(null);
  const [error, setError] = useState<{ message: string; hint?: string } | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);

  const [openKey, setOpenKey] = useState(open);
  if (openKey !== open) {
    setOpenKey(open);
    if (open) {
      setText("");
      setPreview(null);
      setError(null);
    }
  }

  const body = (): AmendBody | null => {
    const instruction = text.trim();
    if (mode === "runtime" && /\+?\s*24\s*h/i.test(instruction)) return { max_runtime_hours: (runtimeHours ?? 72) + 24, instruction: null };
    return instruction ? { instruction } : null;
  };

  // live preview (debounced, cancellable)
  useEffect(() => {
    if (!open) return;
    const b = body();
    if (!b) return;
    const ctrl = new AbortController();
    const timer = setTimeout(async () => {
      setLoading(true);
      try {
        const r = await amendCampaign(campaignId, { ...b, dry_run: true }, ctrl.signal);
        setPreview(r);
        setError(null);
      } catch (e) {
        if ((e as Error).name === "AbortError") return;
        setPreview(null);
        setError({ message: (e as Error).message, hint: (e as ApiError).hint });
      } finally {
        if (!ctrl.signal.aborted) setLoading(false);
      }
    }, 350);
    return () => {
      clearTimeout(timer);
      ctrl.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text, open, campaignId]);

  const resumes = status !== "running" && status !== "planning";
  const hasText = Boolean(text.trim());
  const shown = hasText ? preview : null;
  const shownError = hasText ? error : null;

  async function apply() {
    const b = body();
    if (!b || !preview || preview.noop || saving) return;
    setSaving(true);
    try {
      const r = await amendCampaign(campaignId, { ...b, resume: true, base_hash: preview.base_hash });
      void qc.invalidateQueries({ queryKey: qk.campaign(campaignId) });
      void qc.invalidateQueries({ queryKey: ["campaign-live", campaignId] });
      void qc.invalidateQueries({ queryKey: qk.campaigns });
      const auditId = r.audit_id;
      const refresh = () => {
        void qc.invalidateQueries({ queryKey: qk.campaign(campaignId) });
        void qc.invalidateQueries({ queryKey: ["campaign-live", campaignId] });
        void qc.invalidateQueries({ queryKey: qk.campaigns });
      };
      const action = auditId
        ? {
            label: t.undo,
            onClick: () =>
              void api(`activity/${auditId}/undo`, { method: "POST" })
                .then(() => {
                  toast(t.undone);
                  refresh();
                })
                .catch((e: ApiError) => toast.error(e.message, e.hint ? { description: e.hint } : undefined)),
          }
        : undefined;
      if (r.resume_blocked) {
        toast.warning(t.blocked, { description: [r.resume_blocked.message, r.resume_blocked.hint].filter(Boolean).join(" — "), action });
      } else {
        toast.success(t.done, { description: r.changes.map((c) => `${c.label}: ${c.before} → ${c.after}`).join(" · "), action });
      }
      onApplied?.(r);
      onOpenChange(false);
    } catch (e) {
      const err = e as ApiError;
      if (err.code === "stale_preview") {
        setText((x) => `${x} `); // re-run the preview against the new state
      }
      setError({ message: err.message, hint: err.hint });
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent title={t.title[mode]} description={t.desc} width={520}>
        <div className="space-y-3 px-4 py-3">
          <Textarea
            ref={ref}
            autoFocus
            rows={2}
            value={text}
            placeholder={t.placeholder}
            aria-label={t.title[mode]}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                void apply();
              }
            }}
          />
          <div className="flex flex-wrap gap-1.5">
            {SUGGESTIONS[lang][mode].map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => {
                  setText((x) => (x.trim() ? `${x.trim()}, ${s}` : s));
                  ref.current?.focus();
                }}
                className="press rounded-sm px-2 py-1 text-meta text-fg-2 shadow-[inset_0_0_0_1px_var(--border-subtle)] transition-colors hover:bg-surface-2 hover:text-fg"
              >
                {s}
              </button>
            ))}
          </div>
          <div className="min-h-[76px] rounded-md bg-surface-2/60 px-3 py-2.5 shadow-[inset_0_0_0_1px_var(--border-subtle)]" aria-live="polite">
            <div className="mb-1.5 flex items-center gap-2 text-micro font-medium uppercase tracking-wide text-fg-3">
              {t.preview}
              {loading && hasText && <Spinner size={10} />}
            </div>
            {shownError ? (
              <div className="text-meta">
                <p className="text-warning">{shownError.message}</p>
                {shownError.hint && <p className="mt-0.5 text-fg-3">{shownError.hint}</p>}
              </div>
            ) : shown && shown.changes.length ? (
              <ul className="space-y-1">
                {shown.changes.map((c) => (
                  <li key={c.field} className="grid grid-cols-[92px_1fr] items-baseline gap-2 text-meta animate-fade-in">
                    <span className="text-fg-3">{c.label}</span>
                    <span className="flex min-w-0 flex-wrap items-baseline gap-1.5">
                      <span className="text-fg-3 line-through decoration-fg-3/50">{c.before}</span>
                      <ArrowRight className="size-3 shrink-0 self-center text-fg-3" />
                      <span className="font-medium text-accent-strong">{c.after}</span>
                    </span>
                  </li>
                ))}
                {shown.warnings.map((w) => (
                  <li key={w} className="flex gap-1.5 pt-1 text-meta text-fg-3">
                    <AlertTriangle className="mt-0.5 size-3 shrink-0" />
                    {w}
                  </li>
                ))}
                {shown.requires_pause && <li className="pt-1 text-meta text-fg-3">{t.pauseNote}</li>}
              </ul>
            ) : (
              <p className="text-meta text-fg-3">{t.nothing}</p>
            )}
          </div>
        </div>
        <div className="flex items-center justify-between gap-2 border-t border-line px-4 py-2.5">
          <span className="hidden items-center gap-1 text-meta text-fg-3 sm:flex">
            <Kbd>⌘</Kbd>
            <Kbd>↵</Kbd>
          </span>
          <div className="ml-auto flex gap-2">
            <Button variant="ghost" onClick={() => onOpenChange(false)}>
              {t.cancel}
            </Button>
            <Button variant="primary" className={cn("press")} disabled={!shown || shown.noop || !shown.changes.length || saving || loading} onClick={() => void apply()}>
              {saving ? <Spinner size={12} className="text-accent-contrast" /> : resumes ? <Play /> : <Check />}
              {resumes ? t.applyResume : t.apply}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
