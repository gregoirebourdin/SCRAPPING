"use client";

import { Badge, Button, cn, Kbd, Spinner } from "@scout/design-system";
import type { CampaignDefinition, ParseOut } from "@scout/schemas";
import { useQueryClient } from "@tanstack/react-query";
import { motion } from "motion/react";
import { ArrowUp, Database, MessageSquare, Pencil, Play, Radar } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { StatusText } from "@/components/chat/tool-cards";
import { api } from "@/lib/api";
import { n, relTime } from "@/lib/format";
import { qk, useCampaigns, useMe, type CampaignStatus } from "@/lib/queries";
import { useUI } from "@/lib/store";

const EXAMPLES = [
  "Find 1,000 marketing agencies in France with 2–30 employees, founders with a safe email",
  "Find 500 Shopify stores in Germany and their CEO",
  "Find B2B SaaS startups in London hiring sales people",
  "Find dentists in Lyon with a website and a contact email",
  "Only new agencies I have never scraped, offering TikTok Ads",
];

/** Empty workspace (spec §126) + first-run flow (§127): describe → compact interpretation → start. */
export function Discover() {
  const router = useRouter();
  const qc = useQueryClient();
  const me = useMe();
  const campaigns = useCampaigns();
  const setChatDraft = useUI((s) => s.setChatDraft);
  const [prompt, setPrompt] = useState("");
  const [parsing, setParsing] = useState(false);
  const [parsed, setParsed] = useState<ParseOut | null>(null);
  const [starting, setStarting] = useState(false);
  const [seeding, setSeeding] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);

  useEffect(() => ref.current?.focus(), []);

  async function parse() {
    const text = prompt.trim();
    if (text.length < 3 || parsing) return;
    setParsing(true);
    setParsed(null);
    try {
      setParsed(await api<ParseOut>("campaigns/parse", { body: { prompt: text } }));
    } catch (e) {
      const err = e as Error & { hint?: string };
      toast.error(err.message, err.hint ? { description: err.hint } : undefined);
    } finally {
      setParsing(false);
    }
  }

  async function start(definition: CampaignDefinition) {
    setStarting(true);
    try {
      const c = await api<CampaignStatus>("campaigns", { body: { prompt: prompt.trim(), definition, start: true } });
      void qc.invalidateQueries({ queryKey: qk.campaigns });
      void qc.invalidateQueries({ queryKey: qk.lists });
      toast.success("Discovery started", { description: "Qualified leads will appear as they are verified." });
      router.push(c.target_list_id ? `/lists/${c.target_list_id}` : `/campaigns/${c.id}`);
    } catch (e) {
      const err = e as Error & { hint?: string };
      toast.error(err.message, err.hint ? { description: err.hint } : undefined);
      setStarting(false);
    }
  }

  async function seed() {
    setSeeding(true);
    try {
      const r = await api<{ seeded: boolean; reason?: string; list_id?: string; people?: number; companies?: number }>("dev/seed", { method: "POST" });
      void qc.invalidateQueries();
      if (!r.seeded) {
        toast(r.reason ?? "Demo data already loaded");
        router.push("/people");
        return;
      }
      toast.success("Demo data loaded", { description: `${n(r.people ?? 0)} people · ${n(r.companies ?? 0)} companies (.example domains)` });
      router.push(r.list_id ? `/lists/${r.list_id}` : "/people");
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSeeding(false);
    }
  }

  const recent = (campaigns.data ?? []).slice(0, 5);

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
      <div className="mx-auto flex w-full max-w-[720px] flex-1 flex-col justify-center px-4 py-12">
        <motion.div initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.24, ease: [0.2, 0, 0, 1] }}>
          <h1 className="text-display text-fg">Find your next customers.</h1>
          <p className="mt-1.5 text-body text-fg-3">Describe the companies or people you want to find. Scout only returns leads that are new to you, verified and sourced.</p>

          <form
            className="mt-6 rounded-lg bg-surface-2 shadow-[inset_0_0_0_1px_var(--border-strong)] focus-within:shadow-[inset_0_0_0_1px_var(--accent)]"
            onSubmit={(e) => {
              e.preventDefault();
              void parse();
            }}
          >
            <textarea
              ref={ref}
              value={prompt}
              onChange={(e) => {
                setPrompt(e.target.value);
                if (parsed) setParsed(null);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  void parse();
                }
              }}
              rows={3}
              placeholder="e.g. Find 3,000 new marketing agencies in France with 2–30 employees that offer Instagram or ManyChat. Founder or CEO with a safe email."
              aria-label="Describe who you want to find"
              className="block w-full resize-none bg-transparent px-4 pt-3.5 text-[15px] leading-6 text-fg outline-none placeholder:text-fg-3"
            />
            <div className="flex items-center justify-between px-3 pb-2.5">
              <span className="flex items-center gap-1.5 text-meta text-fg-3">
                <Kbd>↵</Kbd> to plan · <Kbd>⇧</Kbd>
                <Kbd>↵</Kbd> new line
              </span>
              <Button type="submit" size="sm" variant="primary" disabled={prompt.trim().length < 3 || parsing} aria-label="Plan search">
                {parsing ? <Spinner size={12} className="text-accent-contrast" /> : <ArrowUp />} Plan search
              </Button>
            </div>
          </form>

          {parsed && (
            <motion.div initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} className="mt-3 rounded-lg bg-surface-1 shadow-[inset_0_0_0_1px_var(--border-strong)]">
              <div className="flex items-center gap-2 border-b border-line px-4 py-2.5">
                <Radar className="size-4 text-accent" />
                <span className="text-body font-medium text-fg">{parsed.definition.name || "New search"}</span>
                {parsed.parser !== "ai" && (
                  <Badge tone="muted" title="Parsed without AI (deterministic rules)">
                    rules
                  </Badge>
                )}
              </div>
              <dl className="grid grid-cols-[120px_1fr] gap-x-4 gap-y-1.5 px-4 py-3 text-body">
                {parsed.interpretation.map((i) => (
                  <div key={`${i.label}:${i.value}`} className="contents">
                    <dt className="text-fg-3">{i.label}</dt>
                    <dd className="text-fg">{i.value}</dd>
                  </div>
                ))}
              </dl>
              <div className="flex items-center justify-end gap-2 border-t border-line px-4 py-2.5">
                <Button variant="ghost" onClick={() => setChatDraft(`${prompt.trim()}\n\nChange: `)}>
                  <Pencil /> Refine in chat
                </Button>
                <Button variant="primary" disabled={starting} onClick={() => void start(parsed.definition)}>
                  {starting ? <Spinner size={12} className="text-accent-contrast" /> : <Play />} Start discovery
                </Button>
              </div>
            </motion.div>
          )}

          {!parsed && (
            <div className="mt-4 flex flex-wrap gap-1.5">
              {EXAMPLES.map((ex) => (
                <button
                  key={ex}
                  type="button"
                  onClick={() => {
                    setPrompt(ex);
                    ref.current?.focus();
                  }}
                  className="rounded-sm px-2.5 py-1 text-left text-meta text-fg-2 shadow-[inset_0_0_0_1px_var(--border-subtle)] transition-colors hover:bg-surface-2 hover:text-fg"
                >
                  {ex}
                </button>
              ))}
            </div>
          )}

          {recent.length > 0 && (
            <div className="mt-10">
              <h2 className="mb-1.5 text-micro font-medium uppercase tracking-wide text-fg-3">Recent searches</h2>
              <div className="divide-y divide-line rounded-md shadow-[inset_0_0_0_1px_var(--border-subtle)]">
                {recent.map((c) => (
                  <Link key={c.id} href={c.target_list_id ? `/lists/${c.target_list_id}` : `/campaigns/${c.id}`} className="flex items-center gap-3 px-3 py-2 hover:bg-surface-2">
                    <span className="min-w-0 flex-1 truncate text-body text-fg">{c.name}</span>
                    <span className="tabular text-meta text-fg-2">
                      {n(c.qualified)} / {n(c.target)}
                    </span>
                    <StatusText status={c.status} />
                    <span className="w-16 text-right text-meta text-fg-3">{relTime(c.created_at)}</span>
                  </Link>
                ))}
              </div>
            </div>
          )}

          <div className={cn("mt-8 flex items-center gap-3 text-meta text-fg-3", recent.length && "mt-6")}>
            <button type="button" onClick={() => useUI.getState().focusChat()} className="inline-flex items-center gap-1 hover:text-fg">
              <MessageSquare className="size-3.5" /> Or ask the assistant <Kbd>⌘</Kbd>
              <Kbd>J</Kbd>
            </button>
            {me.data?.features?.demo_data && (
              <button type="button" disabled={seeding} onClick={() => void seed()} className="inline-flex items-center gap-1 hover:text-fg disabled:opacity-50">
                {seeding ? <Spinner size={11} /> : <Database className="size-3.5" />} Load demo data
              </button>
            )}
          </div>
        </motion.div>
      </div>
    </div>
  );
}
