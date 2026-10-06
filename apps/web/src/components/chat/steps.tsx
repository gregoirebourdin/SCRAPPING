"use client";

import { cn, ShimmerText, Spinner } from "@scout/design-system";
import { Check, ChevronRight, Minus, RotateCcw, X } from "lucide-react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { useState } from "react";

export interface ChatStep {
  id: string;
  label: string;
  status: "active" | "done" | "failed" | "stopped";
  ms?: number;
  error?: string;
}

function fmtMs(ms: number, lang: "fr" | "en"): string {
  if (ms < 1000) return `${Math.max(1, Math.round(ms / 10) * 10)} ms`;
  const s = (ms / 1000).toFixed(ms < 10_000 ? 1 : 0);
  return lang === "fr" ? `${s.replace(".", ",")} s` : `${s}s`;
}

/** What the assistant is doing, step by step: the active step shimmers; finished ones get a check and their
 * duration; a failed step shows why, with Retry. Once the answer is complete the list folds into one line. */
export function StepList({ steps, pending, lang = "en", onRetry }: { steps: ChatStep[]; pending: boolean; lang?: "fr" | "en"; onRetry?: () => void }) {
  const reduce = useReducedMotion();
  const failed = steps.some((s) => s.status === "failed");
  const [open, setOpen] = useState(false);
  if (!steps.length) return null;
  const total = steps.reduce((a, s) => a + (s.ms ?? 0), 0);
  const collapsed = !pending && !failed && !open;
  if (collapsed) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="press flex items-center gap-1.5 rounded-sm py-0.5 text-meta text-fg-3 transition-colors hover:text-fg-2"
        aria-expanded={false}
      >
        <Check className="size-3 text-success" />
        {lang === "fr" ? `${steps.length} étape${steps.length > 1 ? "s" : ""}` : `${steps.length} step${steps.length > 1 ? "s" : ""}`}
        {total > 0 && <span className="tabular">· {fmtMs(total, lang)}</span>}
        <ChevronRight className="size-3" />
      </button>
    );
  }
  return (
    <div className="space-y-1" role="status" aria-live="polite">
      <AnimatePresence initial={false}>
        {steps.map((s) => (
          <motion.div
            key={s.id}
            initial={reduce ? false : { opacity: 0, y: 3 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.16, ease: [0.2, 0.8, 0.2, 1] }}
            className="flex min-w-0 items-start gap-2 text-meta"
          >
            <span className="mt-px grid size-4 shrink-0 place-items-center">
              {s.status === "active" ? (
                <Spinner size={11} className="text-accent" />
              ) : s.status === "failed" ? (
                <X className="size-3.5 text-danger" />
              ) : s.status === "stopped" ? (
                <Minus className="size-3.5 text-fg-3" />
              ) : (
                <Check className="size-3.5 text-success" />
              )}
            </span>
            <span className="min-w-0 flex-1">
              {s.status === "active" ? (
                <ShimmerText className="text-body">{s.label}</ShimmerText>
              ) : (
                <span className={cn("text-body", s.status === "failed" ? "text-danger" : "text-fg-3")}>{s.label}</span>
              )}
              {s.status === "failed" && s.error && <span className="block text-meta text-fg-3">{s.error}</span>}
            </span>
            {s.ms != null && s.status !== "active" && <span className="tabular mt-0.5 shrink-0 text-fg-3">{fmtMs(s.ms, lang)}</span>}
          </motion.div>
        ))}
      </AnimatePresence>
      {failed && onRetry && !pending && (
        <button type="button" onClick={onRetry} className="press ml-6 inline-flex items-center gap-1 rounded-sm px-1.5 py-0.5 text-meta text-accent-strong hover:bg-surface-2">
          <RotateCcw className="size-3" /> {lang === "fr" ? "Réessayer" : "Retry"}
        </button>
      )}
      {!pending && !failed && open && (
        <button type="button" onClick={() => setOpen(false)} className="ml-6 text-meta text-fg-3 hover:text-fg-2">
          {lang === "fr" ? "Masquer" : "Hide"}
        </button>
      )}
    </div>
  );
}
