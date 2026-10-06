"use client";

import { Badge, Button, cn, Dialog, DialogContent, Input, Segmented, Spinner, Textarea } from "@scout/design-system";
import type { ColumnOut, RowRef } from "@scout/schemas";
import { useQueryClient } from "@tanstack/react-query";
import { Coins, Database, Sparkles, Wand2 } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import { n } from "@/lib/format";
import { useColumns } from "@/lib/queries";

import { enrichColumn, invalidateRows } from "./actions";

type DataType = "auto" | "boolean" | "text" | "number" | "url" | "enum";

interface PlanOut {
  plan: {
    data_type: string;
    strategy: string;
    resolver: string;
    entity_type: string;
    keywords: string[];
    technologies: string[];
    enum_values: string[];
    confidence_threshold: number;
    refresh_days: number;
  };
  describe: { resolver_label: string; sources_label: string; cost_label: string; kind: string; explanation: string };
}

const EXAMPLES = [
  { name: "Uses Shopify?", instruction: "Does the company's website run on Shopify?" },
  { name: "Mentions ManyChat", instruction: "Does the website mention ManyChat or Instagram DM automation?" },
  { name: "Offers SEO", instruction: "Does the agency offer SEO services?" },
  { name: "Hiring", instruction: "Is the company currently hiring? Look for a careers page with open roles." },
  { name: "Main service", instruction: "In 3–6 words, what is the company's main service?" },
];

/** "Add column" (spec §39–§44): describe a column in plain language; the planner picks the cheapest reliable resolver. */
interface AddColumnProps {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  listId: string | null;
  rows?: RowRef | null;
  entityType: "person" | "company";
}

export function AddColumnDialog(props: AddColumnProps) {
  return (
    <Dialog open={props.open} onOpenChange={props.onOpenChange}>
      {props.open && <AddColumnBody {...props} />}
    </Dialog>
  );
}

function AddColumnBody({ onOpenChange, listId, rows, entityType }: AddColumnProps) {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [instruction, setInstruction] = useState("");
  const [dataType, setDataType] = useState<DataType>("auto");
  const [planned, setPlanned] = useState<{ key: string; plan: PlanOut | null } | null>(null);
  const [creating, setCreating] = useState(false);
  const planKey = JSON.stringify([name.trim(), instruction.trim(), dataType, listId]);

  // Debounced plan preview — deterministic planning is free; AI planning only for vague asks.
  useEffect(() => {
    if (!name.trim()) return;
    const key = planKey;
    const t = setTimeout(async () => {
      try {
        const p = await api<PlanOut>("columns/plan", {
          body: { name: name.trim(), instruction: instruction.trim() || null, data_type: dataType === "auto" ? null : dataType, list_id: listId },
        });
        setPlanned({ key, plan: p });
      } catch {
        setPlanned({ key, plan: null });
      }
    }, 450);
    return () => clearTimeout(t);
  }, [planKey, name, instruction, dataType, listId]);
  const plan = planned?.key === planKey ? planned.plan : null;
  const planning = Boolean(name.trim()) && planned?.key !== planKey;

  async function create(run: boolean) {
    setCreating(true);
    try {
      const r = await api<{ column: ColumnOut; queued: number; coverage: { total?: number; cached?: number } }>("columns", {
        body: { name: name.trim(), instruction: instruction.trim() || null, data_type: dataType === "auto" ? null : dataType, list_id: listId, rows: rows ?? null, run },
      });
      toast.success(`Column “${r.column.name}” created`, {
        description: run ? `${n(r.queued)} rows queued${r.coverage?.cached ? ` · ${n(r.coverage.cached)} reused from cache` : ""}` : "Not run yet — use Enrich when ready",
      });
      void qc.invalidateQueries({ queryKey: ["columns"] });
      void qc.invalidateQueries({ queryKey: ["fields"] });
      invalidateRows(qc);
      onOpenChange(false);
    } catch (e) {
      const err = e as Error & { hint?: string };
      toast.error(err.message, err.hint ? { description: err.hint } : undefined);
    } finally {
      setCreating(false);
    }
  }

  const companyLevel = plan?.plan.entity_type === "company" && entityType === "person";

  return (
    <DialogContent title="Add a column" description="Describe what you want to know. Scout picks the cheapest reliable method and reuses cached data." width={560}>
      <div className="space-y-3 px-4 py-3">
        <div className="grid grid-cols-[96px_1fr] items-center gap-x-3 gap-y-2.5">
          <label htmlFor="col-name" className="text-meta text-fg-3">
            Name
          </label>
          <Input id="col-name" autoFocus placeholder="e.g. Uses Shopify?" value={name} onChange={(e) => setName(e.target.value)} />
          <label htmlFor="col-instr" className="self-start pt-1.5 text-meta text-fg-3">
            Instruction
          </label>
          <Textarea
            id="col-instr"
            rows={3}
            placeholder="Optional — e.g. Does the website mention ManyChat or Instagram automation?"
            value={instruction}
            onChange={(e) => setInstruction(e.target.value)}
          />
          <span className="text-meta text-fg-3">Type</span>
          <Segmented<DataType>
            value={dataType}
            onChange={setDataType}
            options={[
              { value: "auto", label: "Auto" },
              { value: "boolean", label: "Yes / No" },
              { value: "text", label: "Text" },
              { value: "number", label: "Number" },
              { value: "url", label: "URL" },
              { value: "enum", label: "Category" },
            ]}
          />
        </div>
        {!name.trim() && (
          <div className="flex flex-wrap gap-1.5 pt-1">
            {EXAMPLES.map((ex) => (
              <button
                key={ex.name}
                type="button"
                onClick={() => {
                  setName(ex.name);
                  setInstruction(ex.instruction);
                }}
                className="rounded-sm px-2 py-1 text-meta text-fg-2 shadow-[inset_0_0_0_1px_var(--border-strong)] hover:bg-surface-2 hover:text-fg"
              >
                {ex.name}
              </button>
            ))}
          </div>
        )}
        {name.trim() && (
          <div className="rounded-md bg-surface-2 p-3 shadow-[inset_0_0_0_1px_var(--border-subtle)]">
            <div className="mb-2 flex items-center gap-2 text-meta font-medium text-fg-2">
              <Wand2 className="size-3.5 text-accent" /> Enrichment plan {planning && <Spinner size={11} />}
            </div>
            {plan ? (
              <dl className="grid grid-cols-[96px_1fr] gap-x-3 gap-y-1 text-meta">
                <dt className="text-fg-3">Method</dt>
                <dd className="text-fg">{plan.describe.resolver_label}</dd>
                <dt className="text-fg-3">Sources</dt>
                <dd className="flex items-center gap-1.5 text-fg-2">
                  <Database className="size-3 text-fg-3" />
                  {plan.describe.sources_label}
                </dd>
                <dt className="text-fg-3">Cost</dt>
                <dd className="flex items-center gap-1.5 text-fg-2">
                  <Coins className="size-3 text-fg-3" />
                  {plan.describe.cost_label}
                </dd>
                <dt className="text-fg-3">Output</dt>
                <dd className="flex flex-wrap items-center gap-1 text-fg-2">
                  <Badge tone="neutral">{plan.plan.data_type}</Badge>
                  <Badge tone={plan.describe.kind === "factual" ? "success" : plan.describe.kind === "generated" ? "info" : "warning"}>{plan.describe.kind}</Badge>
                  {companyLevel && <Badge tone="muted">company-level</Badge>}
                  <span className="text-fg-3">· unknown when evidence is below {Math.round((plan.plan.confidence_threshold ?? 0.8) * 100)}%</span>
                </dd>
                {(plan.plan.keywords?.length ?? 0) > 0 && (
                  <>
                    <dt className="text-fg-3">Looks for</dt>
                    <dd className="truncate text-fg-2">{plan.plan.keywords.slice(0, 8).join(", ")}</dd>
                  </>
                )}
                {(plan.plan.technologies?.length ?? 0) > 0 && (
                  <>
                    <dt className="text-fg-3">Technologies</dt>
                    <dd className="truncate text-fg-2">{plan.plan.technologies.join(", ")}</dd>
                  </>
                )}
                {plan.describe.explanation && <dd className="col-span-2 pt-1 text-fg-3">{plan.describe.explanation}</dd>}
              </dl>
            ) : (
              <p className="text-meta text-fg-3">{planning ? "Planning…" : "Type a name to preview how it will be computed."}</p>
            )}
          </div>
        )}
      </div>
      <div className="flex items-center justify-between gap-2 border-t border-line px-4 py-3">
        <span className="text-meta text-fg-3">{rows ? "Runs on the selected rows" : listId ? "Runs on this list" : "Runs on all matching leads"}</span>
        <div className="flex gap-2">
          <Button variant="ghost" disabled={!name.trim() || creating} onClick={() => void create(false)}>
            Create only
          </Button>
          <Button variant="primary" disabled={!name.trim() || creating} onClick={() => void create(true)}>
            {creating ? <Spinner size={12} className="text-accent-contrast" /> : <Sparkles />} Create & enrich
          </Button>
        </div>
      </div>
    </DialogContent>
  );
}

/** "View configuration" for a custom column: method, sources, threshold, refresh policy (spec §44, §122). */
export function ColumnConfigDialog({ columnId, listId, onOpenChange }: { columnId: string | null; listId: string | null; onOpenChange: (v: boolean) => void }) {
  const columns = useColumns(listId);
  const col = (columns.data ?? []).find((c) => c.id === columnId);
  return (
    <Dialog open={Boolean(columnId)} onOpenChange={onOpenChange}>
      {columnId && (
        <DialogContent title={col ? col.name : "Column"} description="How this column is computed" width={540}>
          {col ? (
            <ColumnConfigForm key={col.id} col={col} listId={listId} onClose={() => onOpenChange(false)} />
          ) : (
            <div className="grid h-32 place-items-center">
              <Spinner />
            </div>
          )}
        </DialogContent>
      )}
    </Dialog>
  );
}

function ColumnConfigForm({ col, listId, onClose }: { col: ColumnOut; listId: string | null; onClose: () => void }) {
  const qc = useQueryClient();
  const onOpenChange = (v: boolean) => !v && onClose();
  const [instruction, setInstruction] = useState(col.instructions ?? "");
  const [threshold, setThreshold] = useState(Math.round((col.confidence_threshold ?? 0.8) * 100));
  const [refreshDays, setRefreshDays] = useState(Number((col.refresh_policy as { days?: number })?.days ?? (col.configuration as { refresh_days?: number })?.refresh_days ?? 30));
  const [saving, setSaving] = useState(false);
  const cfg = (col.configuration ?? {}) as Record<string, unknown>;

  async function save(rerun: boolean) {
    setSaving(true);
    try {
      await api(`columns/${col.id}`, {
        method: "PATCH",
        body: {
          instruction: instruction.trim() !== (col.instructions ?? "") ? instruction.trim() : null,
          confidence_threshold: threshold / 100,
          refresh_days: refreshDays,
        },
      });
      void qc.invalidateQueries({ queryKey: ["columns"] });
      if (rerun) await enrichColumn(qc, col.id, col.name, null, { force: true, onlyMissing: false, listId });
      else toast("Column updated");
      invalidateRows(qc);
      onOpenChange(false);
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <div className="space-y-3 px-4 py-3">
        <dl className="grid grid-cols-[120px_1fr] gap-x-3 gap-y-1.5 text-meta">
          <dt className="text-fg-3">Resolver</dt>
          <dd className="text-fg">
            {col.resolver_type.replace(/_/g, " ")}
            {cfg.strategy ? <span className="text-fg-3"> · {String(cfg.strategy).replace(/_/g, " ")}</span> : null}
          </dd>
          <dt className="text-fg-3">Type</dt>
          <dd className="flex gap-1">
            <Badge tone="neutral">{col.data_type}</Badge>
            <Badge tone={col.kind === "factual" ? "success" : col.kind === "generated" ? "info" : "warning"}>{col.kind}</Badge>
            <Badge tone="muted">{col.entity_type}</Badge>
          </dd>
          {Array.isArray(cfg.keywords) && cfg.keywords.length > 0 && (
            <>
              <dt className="text-fg-3">Keywords</dt>
              <dd className="text-fg-2">{(cfg.keywords as string[]).join(", ")}</dd>
            </>
          )}
          {Array.isArray(cfg.input_sources) && cfg.input_sources.length > 0 && (
            <>
              <dt className="text-fg-3">Pages</dt>
              <dd className="text-fg-2">{(cfg.input_sources as string[]).join(", ").replace(/_/g, " ")}</dd>
            </>
          )}
          {Array.isArray(cfg.technologies) && cfg.technologies.length > 0 && (
            <>
              <dt className="text-fg-3">Technologies</dt>
              <dd className="text-fg-2">{(cfg.technologies as string[]).join(", ")}</dd>
            </>
          )}
          {cfg.explanation ? (
            <>
              <dt className="text-fg-3">Why</dt>
              <dd className="text-fg-2">{String(cfg.explanation)}</dd>
            </>
          ) : null}
        </dl>
        <div className="space-y-1.5">
          <label htmlFor="cfg-instr" className="text-meta text-fg-3">
            Instruction
          </label>
          <Textarea id="cfg-instr" rows={3} value={instruction} onChange={(e) => setInstruction(e.target.value)} />
          <p className="text-micro text-fg-3">Changing the instruction re-plans the column; existing values become stale.</p>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-1.5">
            <label htmlFor="cfg-th" className="text-meta text-fg-3">
              Confidence threshold
            </label>
            <div className="flex items-center gap-2">
              <input
                id="cfg-th"
                type="range"
                min={50}
                max={99}
                value={threshold}
                onChange={(e) => setThreshold(Number(e.target.value))}
                className={cn("flex-1 accent-[var(--accent)]")}
              />
              <span className="tabular w-9 text-right text-meta text-fg">{threshold}%</span>
            </div>
          </div>
          <div className="space-y-1.5">
            <label htmlFor="cfg-rd" className="text-meta text-fg-3">
              Refresh after (days)
            </label>
            <Input id="cfg-rd" type="number" min={1} max={365} value={refreshDays} onChange={(e) => setRefreshDays(Math.max(1, Number(e.target.value) || 30))} />
          </div>
        </div>
      </div>
      <div className="flex justify-end gap-2 border-t border-line px-4 py-3">
        <Button variant="ghost" onClick={() => onOpenChange(false)}>
          Cancel
        </Button>
        <Button disabled={saving} onClick={() => void save(false)}>
          Save
        </Button>
        <Button variant="primary" disabled={saving} onClick={() => void save(true)}>
          <Sparkles /> Save & re-run
        </Button>
      </div>
    </>
  );
}
