"use client";

import { Button, cn, Spinner } from "@scout/design-system";
import { ArrowRight, Check, CornerDownLeft, MessageCircleQuestion } from "lucide-react";
import { useId, useRef, useState } from "react";

export interface ClarifyOption {
  value: string;
  label: string;
}
export interface ClarifyQuestion {
  id: string;
  text: string;
  options: ClarifyOption[];
  allow_free_text?: boolean;
  default?: string | null;
}
export interface ClarifyAnswer {
  id: string;
  value: string;
  label?: string;
  question?: string;
}
export interface ClarifySubmit {
  answers: ClarifyAnswer[];
  use_defaults?: boolean;
  skipped?: boolean;
  summary: string;
}

const COPY = {
  en: {
    questions: "A few details",
    other: "Other…",
    otherPh: "Type your answer",
    defaults: "Use defaults",
    skip: "Skip",
    go: "Continue",
    default: "default",
    answered: "Answered",
  },
  fr: {
    questions: "Quelques précisions",
    other: "Autre…",
    otherPh: "Ta réponse",
    defaults: "Valeurs par défaut",
    skip: "Passer",
    go: "Continuer",
    default: "défaut",
    answered: "Répondu",
  },
};

/** ≤ 3 questions, one round. Option chips are radio groups (arrows move, Enter/Space picks); "Other…" opens a
 * free-text field; Continue fills the questions left open with their defaults. */
export function ClarifyCard({
  card,
  disabled,
  onSubmit,
}: {
  card: Record<string, unknown> & { questions?: ClarifyQuestion[]; lang?: string; answered?: boolean; answer_text?: string | null };
  disabled?: boolean;
  onSubmit: (s: ClarifySubmit) => void;
}) {
  const lang = card.lang === "fr" ? "fr" : "en";
  const t = COPY[lang];
  const questions = card.questions ?? [];
  const [answers, setAnswers] = useState<Record<string, ClarifyAnswer>>({});
  const [other, setOther] = useState<Record<string, boolean>>({});
  const [sent, setSent] = useState(false);
  const uid = useId();

  if (card.answered || sent) {
    const text =
      card.answer_text ??
      Object.values(answers)
        .map((a) => a.label ?? a.value)
        .join(" · ");
    return (
      <div className="flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-meta text-fg-3 shadow-[inset_0_0_0_1px_var(--border-subtle)] animate-fade-in">
        {sent && !card.answered ? <Spinner size={11} /> : <Check className="size-3.5 text-success" />}
        <span className="truncate">{text || t.answered}</span>
      </div>
    );
  }

  function pick(q: ClarifyQuestion, opt: ClarifyOption) {
    setAnswers((a) => ({ ...a, [q.id]: { id: q.id, value: opt.value, label: opt.label, question: q.text } }));
    setOther((o) => ({ ...o, [q.id]: false }));
  }

  function submit(kind: "answers" | "defaults" | "skip") {
    if (disabled || sent) return;
    setSent(true);
    const list = Object.values(answers).filter((a) => a.value.trim());
    if (kind === "skip") return onSubmit({ answers: [], skipped: true, summary: t.skip });
    const filled = [...list];
    for (const q of questions) {
      if (!filled.some((a) => a.id === q.id) && q.default) {
        const o = q.options.find((x) => x.value === q.default);
        filled.push({ id: q.id, value: q.default, label: o?.label ?? q.default, question: q.text });
      }
    }
    const order = new Map(questions.map((q, i) => [q.id, i]));
    filled.sort((a, b) => (order.get(a.id) ?? 0) - (order.get(b.id) ?? 0));
    onSubmit({
      answers: kind === "defaults" ? [] : list,
      use_defaults: true,
      summary: filled.map((a) => a.label ?? a.value).join(" · ") || t.defaults,
    });
  }

  return (
    <div
      className="rounded-md bg-surface-1 shadow-[inset_0_0_0_1px_var(--border-strong)] animate-fade-in"
      onKeyDown={(e) => e.key === "Enter" && (e.metaKey || e.ctrlKey) && submit("answers")}
    >
      <div className="flex items-center gap-2 px-3 pt-2.5 text-micro font-medium uppercase tracking-wide text-fg-3">
        <MessageCircleQuestion className="size-3.5 text-accent" />
        {t.questions} · {questions.length}
      </div>
      <div className="space-y-3 px-3 py-2.5">
        {questions.map((q, qi) => (
          <Question
            key={q.id}
            id={`${uid}-${q.id}`}
            index={qi}
            q={q}
            t={t}
            value={answers[q.id]}
            otherOpen={Boolean(other[q.id])}
            onPick={(o) => pick(q, o)}
            onOther={() => setOther((o) => ({ ...o, [q.id]: true }))}
            onText={(v) => setAnswers((a) => ({ ...a, [q.id]: { id: q.id, value: v, label: v, question: q.text } }))}
          />
        ))}
      </div>
      <div className="flex flex-wrap items-center gap-1.5 border-t border-line px-3 py-2">
        <Button size="xs" variant="ghost" className="press" disabled={disabled} onClick={() => submit("defaults")}>
          {t.defaults}
        </Button>
        <Button size="xs" variant="ghost" className="press" disabled={disabled} onClick={() => submit("skip")}>
          {t.skip}
        </Button>
        <span className="flex-1" />
        <Button size="xs" variant="primary" className="press" disabled={disabled} onClick={() => submit("answers")}>
          {t.go} <ArrowRight />
        </Button>
      </div>
    </div>
  );
}

function Question({
  id,
  index,
  q,
  t,
  value,
  otherOpen,
  onPick,
  onOther,
  onText,
}: {
  id: string;
  index: number;
  q: ClarifyQuestion;
  t: (typeof COPY)["en"];
  value?: ClarifyAnswer;
  otherOpen: boolean;
  onPick: (o: ClarifyOption) => void;
  onOther: () => void;
  onText: (v: string) => void;
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);
  const opts = q.options;
  const n = opts.length + (q.allow_free_text ? 1 : 0);
  const selectedIdx = otherOpen ? opts.length : opts.findIndex((o) => o.value === value?.value);
  const focusIdx = selectedIdx >= 0 ? selectedIdx : 0;

  function move(from: number, d: number) {
    const to = (from + d + n) % n;
    refs.current[to]?.focus();
    if (to < opts.length) onPick(opts[to]!);
    else onOther();
  }

  return (
    <div>
      <p id={`${id}-label`} className="mb-1.5 text-body text-fg">
        <span className="mr-1.5 tabular text-fg-3">{index + 1}.</span>
        {q.text}
      </p>
      <div role="radiogroup" aria-labelledby={`${id}-label`} className="flex flex-wrap gap-1.5">
        {opts.map((o, i) => {
          const on = !otherOpen && value?.value === o.value;
          return (
            <button
              key={o.value}
              ref={(el) => {
                refs.current[i] = el;
              }}
              type="button"
              role="radio"
              aria-checked={on}
              tabIndex={i === focusIdx ? 0 : -1}
              onClick={() => onPick(o)}
              onKeyDown={(e) => {
                if (e.key === "ArrowRight" || e.key === "ArrowDown") {
                  e.preventDefault();
                  move(i, 1);
                } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
                  e.preventDefault();
                  move(i, -1);
                }
              }}
              className={cn(
                "press inline-flex h-7 items-center gap-1 rounded-sm px-2.5 text-meta font-medium outline-none transition-[background-color,color,box-shadow] duration-[var(--dur-1)] focus-visible:shadow-focus",
                on
                  ? "bg-accent-soft text-accent-strong shadow-[inset_0_0_0_1px_var(--border-focus)]"
                  : "text-fg-2 shadow-[inset_0_0_0_1px_var(--border-strong)] hover:bg-surface-2 hover:text-fg",
              )}
            >
              {on && <Check className="size-3" />}
              {o.label}
              {q.default === o.value && !on && <span className="ml-0.5 text-micro font-normal text-fg-3">· {t.default}</span>}
            </button>
          );
        })}
        {q.allow_free_text && (
          <button
            ref={(el) => {
              refs.current[opts.length] = el;
            }}
            type="button"
            role="radio"
            aria-checked={otherOpen}
            tabIndex={focusIdx === opts.length ? 0 : -1}
            onClick={() => {
              onOther();
              requestAnimationFrame(() => inputRef.current?.focus());
            }}
            onKeyDown={(e) => {
              if (e.key === "ArrowRight" || e.key === "ArrowDown") {
                e.preventDefault();
                move(opts.length, 1);
              } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
                e.preventDefault();
                move(opts.length, -1);
              }
            }}
            className={cn(
              "press inline-flex h-7 items-center rounded-sm px-2.5 text-meta outline-none focus-visible:shadow-focus",
              otherOpen ? "bg-accent-soft text-accent-strong" : "text-fg-3 shadow-[inset_0_0_0_1px_var(--border-subtle)] hover:bg-surface-2 hover:text-fg",
            )}
          >
            {t.other}
          </button>
        )}
      </div>
      {otherOpen && (
        <div className="relative mt-1.5 animate-fade-in">
          <input
            ref={inputRef}
            aria-label={q.text}
            placeholder={t.otherPh}
            defaultValue={value && !opts.some((o) => o.value === value.value) ? value.value : ""}
            onChange={(e) => onText(e.target.value)}
            className="h-7 w-full rounded-sm bg-surface-2 pl-2.5 pr-7 text-meta text-fg outline-none shadow-[inset_0_0_0_1px_var(--border-strong)] placeholder:text-fg-3 focus:shadow-[inset_0_0_0_1px_var(--border-focus)]"
          />
          <CornerDownLeft className="pointer-events-none absolute right-2 top-1/2 size-3 -translate-y-1/2 text-fg-3" />
        </div>
      )}
    </div>
  );
}
