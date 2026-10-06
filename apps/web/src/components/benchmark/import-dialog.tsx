"use client";

import { Badge, Button, cn, DialogContent, Input, Segmented, Spinner, Switch, Textarea } from "@scout/design-system";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, CircleAlert, Download, FileSpreadsheet, Upload } from "lucide-react";
import { useRef, useState } from "react";
import { toast } from "sonner";

import { api, type ApiError } from "@/lib/api";
import { n } from "@/lib/format";

import type { Dataset, ImportPreview } from "./types";

interface ColumnsDoc {
  columns: Record<string, string>;
  limits: { csv_bytes: number; rows: number; items: number; people_per_item: number; enrichment_columns: number };
}

type RowError = { where?: string; field?: string; message?: string };

const KINDS = [
  { value: "leads", label: "Leads" },
  { value: "email", label: "Emails" },
  { value: "enrichment", label: "Enrichment" },
] as const;

const KIND_HINT: Record<string, string> = {
  leads: "Companies → decision makers → emails (+ optional enrichment). The engine must find the people.",
  email: "People are given to the engine by name; only their addresses and statuses are measured.",
  enrichment: "Companies with expected column values (enrich:<column> headers).",
};

/** Ground-truth import: CSV upload or paste → server-side preview (validation, mapping, counts) → create. */
export function ImportDatasetFlow({ onDone }: { onDone: (d: Dataset) => void }) {
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [name, setName] = useState("");
  const [kind, setKind] = useState<"leads" | "email" | "enrichment">("leads");
  const [csv, setCsv] = useState("");
  const [fileName, setFileName] = useState<string | null>(null);
  const [exhaustive, setExhaustive] = useState(true);
  const [companyInput, setCompanyInput] = useState<"domain" | "name">("domain");
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [errors, setErrors] = useState<RowError[]>([]);
  const [busy, setBusy] = useState(false);
  const [drag, setDrag] = useState(false);
  const docs = useQuery({ queryKey: ["benchmark", "columns"], queryFn: () => api<ColumnsDoc>("benchmark/columns"), staleTime: Infinity });

  const body = () => ({ kind, csv, people_exhaustive: exhaustive, company_input: companyInput });

  function fail(e: unknown) {
    const err = e as ApiError;
    const details = (err.details as { errors?: RowError[] } | undefined)?.errors ?? [];
    setErrors(details);
    if (!details.length) toast.error(err.message, err.hint ? { description: err.hint } : undefined);
  }

  async function readFile(f: File) {
    if (!/\.(csv|tsv|txt)$/i.test(f.name)) {
      toast.error("Please choose a CSV file");
      return;
    }
    if (docs.data && f.size > docs.data.limits.csv_bytes) {
      toast.error(`File too large (max ${Math.round(docs.data.limits.csv_bytes / 1_000_000)} MB)`);
      return;
    }
    setCsv(await f.text());
    setFileName(f.name);
    if (!name) setName(f.name.replace(/\.(csv|tsv|txt)$/i, ""));
    setPreview(null);
    setErrors([]);
  }

  async function runPreview() {
    setBusy(true);
    setErrors([]);
    try {
      setPreview(await api<ImportPreview>("benchmark/datasets/preview", { body: body() }));
    } catch (e) {
      setPreview(null);
      fail(e);
    } finally {
      setBusy(false);
    }
  }

  async function create() {
    setBusy(true);
    try {
      const d = await api<Dataset>("benchmark/datasets", { body: { ...body(), name: name.trim() } });
      toast.success(`Imported ${n(d.item_count)} ground-truth items`);
      void qc.invalidateQueries({ queryKey: ["benchmark", "datasets"] });
      onDone(d);
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <DialogContent title="Import ground truth" description="Expected companies, people, emails and column values — compared with what the engine finds." width={760}>
      <div className="max-h-[70vh] space-y-3 overflow-y-auto p-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="block min-w-0">
            <span className="mb-1 block text-meta text-fg-3">Name</span>
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Lyon agencies — verified" maxLength={120} />
          </label>
          <div className="min-w-0">
            <span className="mb-1 block text-meta text-fg-3">Kind</span>
            <Segmented
              value={kind}
              onChange={(v) => {
                setKind(v);
                setPreview(null);
              }}
              options={KINDS.map((k) => ({ value: k.value, label: k.label }))}
            />
            <p className="mt-1 text-micro text-fg-3">{KIND_HINT[kind]}</p>
          </div>
        </div>

        {!preview ? (
          <>
            <div className="grid gap-2 sm:grid-cols-[1fr_auto]">
              <button
                type="button"
                onClick={() => fileRef.current?.click()}
                onDragOver={(e) => {
                  e.preventDefault();
                  setDrag(true);
                }}
                onDragLeave={() => setDrag(false)}
                onDrop={(e) => {
                  e.preventDefault();
                  setDrag(false);
                  const f = e.dataTransfer.files[0];
                  if (f) void readFile(f);
                }}
                className={cn(
                  "flex min-h-16 w-full items-center justify-center gap-2 rounded-md border border-dashed px-3 py-3 text-center text-body transition-colors",
                  drag ? "border-accent bg-accent-soft" : "border-line-strong hover:bg-surface-2",
                )}
              >
                {fileName ? <FileSpreadsheet className="size-4 shrink-0 text-fg-3" /> : <Upload className="size-4 shrink-0 text-fg-3" />}
                <span className="min-w-0 truncate text-fg">{fileName ?? "Drop a CSV or click to choose"}</span>
              </button>
              <a
                href="/api/v1/benchmark/template.csv"
                download
                className="inline-flex h-8 items-center justify-center gap-1.5 rounded-sm px-3 text-body text-fg-2 shadow-[inset_0_0_0_1px_var(--border-strong)] hover:bg-surface-2 hover:text-fg"
              >
                <Download className="size-3.5" /> Template
              </a>
              <input ref={fileRef} type="file" accept=".csv,.tsv,.txt,text/csv" className="hidden" onChange={(e) => e.target.files?.[0] && void readFile(e.target.files[0])} />
            </div>
            <label className="block">
              <span className="mb-1 block text-meta text-fg-3">…or paste CSV</span>
              <Textarea
                value={csv}
                onChange={(e) => {
                  setCsv(e.target.value);
                  setFileName(null);
                  setErrors([]);
                }}
                rows={6}
                spellCheck={false}
                placeholder={
                  "company_domain,company_name,person_first,person_last,person_title,email,email_status\nacme-demo.fr,Acme Demo,Marie,Dupont,Fondatrice,marie.dupont@acme-demo.fr,SAFE"
                }
                className="font-mono text-[12px]"
              />
            </label>
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="flex items-start justify-between gap-3 text-body text-fg-2">
                <span>
                  Lists every decision maker
                  <span className="block text-meta text-fg-3">Off: extra people found are not counted as false positives</span>
                </span>
                <Switch checked={exhaustive} onCheckedChange={setExhaustive} label="Ground truth lists every decision maker" />
              </label>
              <div className="text-body text-fg-2">
                <span className="mb-1 block">Engine input</span>
                <Segmented
                  value={companyInput}
                  onChange={setCompanyInput}
                  options={[
                    { value: "domain", label: "Domain" },
                    { value: "name", label: "Name only" },
                  ]}
                />
                <span className="mt-1 block text-meta text-fg-3">
                  {companyInput === "domain" ? "Identity given: company precision/recall are not measured" : "The engine must find the website: company precision/recall measured"}
                </span>
              </div>
            </div>
            <details className="rounded-md bg-surface-2/50 px-3 py-2 text-meta text-fg-3">
              <summary className="cursor-pointer text-fg-2">CSV columns</summary>
              <dl className="mt-2 grid gap-x-3 gap-y-1 sm:grid-cols-[auto_1fr]">
                {Object.entries(docs.data?.columns ?? {}).map(([k, v]) => (
                  <div key={k} className="contents">
                    <dt className="font-mono text-[11px] text-fg-2">{k}</dt>
                    <dd className="mb-1 sm:mb-0">{v}</dd>
                  </div>
                ))}
              </dl>
              {docs.data && (
                <p className="mt-2">
                  One row per expected person; rows of the same company are merged. Limits: {n(docs.data.limits.rows)} rows, {n(docs.data.limits.items)} companies,{" "}
                  {Math.round(docs.data.limits.csv_bytes / 1_000_000)} MB. French headers (prénom, nom, poste…) are recognized.
                </p>
              )}
            </details>
          </>
        ) : (
          <PreviewPanel preview={preview} />
        )}

        {errors.length > 0 && (
          <div className="rounded-md bg-danger-soft px-3 py-2 text-meta text-danger">
            <div className="mb-1 flex items-center gap-1.5 font-medium">
              <CircleAlert className="size-3.5" /> {errors.length} problem{errors.length > 1 ? "s" : ""} — nothing was imported
            </div>
            <ul className="max-h-32 space-y-0.5 overflow-y-auto">
              {errors.slice(0, 30).map((e, i) => (
                <li key={i} className="break-words">
                  <span className="font-medium">{e.where}</span>
                  {e.field ? ` · ${e.field}` : ""}: {e.message}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
      <div className="flex flex-wrap items-center justify-end gap-2 border-t border-line px-4 py-3">
        {preview ? (
          <>
            <Button variant="ghost" onClick={() => setPreview(null)} className="mr-auto">
              <ArrowLeft /> Edit
            </Button>
            <Button variant="primary" disabled={busy || !name.trim()} onClick={() => void create()}>
              {busy ? <Spinner size={12} className="text-accent-contrast" /> : <Upload />} Import {n(preview.summary.items)} items
            </Button>
          </>
        ) : (
          <Button variant="primary" disabled={busy || !csv.trim()} onClick={() => void runPreview()}>
            {busy ? <Spinner size={12} className="text-accent-contrast" /> : null} Preview
          </Button>
        )}
      </div>
    </DialogContent>
  );
}

function PreviewPanel({ preview }: { preview: ImportPreview }) {
  const s = preview.summary;
  return (
    <div className="space-y-3">
      <dl className="grid grid-cols-2 gap-2 text-center sm:grid-cols-5">
        {(
          [
            ["Companies", s.items],
            ["People", s.people],
            ["Emails", s.emails],
            ["No mailbox", s.invalid_emails],
            ["Column values", s.enrichment_values],
          ] as const
        ).map(([label, v]) => (
          <div key={label} className="rounded-sm bg-surface-2 px-2 py-1.5">
            <dt className="text-micro text-fg-3">{label}</dt>
            <dd className="tabular text-body font-medium text-fg">{n(v ?? 0)}</dd>
          </div>
        ))}
      </dl>
      {Object.keys(preview.mapping).length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {Object.entries(preview.mapping).map(([h, t]) => (
            <Badge key={h} tone="neutral" title={`${h} → ${t}`} className="max-w-full">
              {h === t ? t : `${h} → ${t}`}
            </Badge>
          ))}
        </div>
      )}
      {preview.warnings.map((w) => (
        <p key={w} className="text-meta text-warning">
          {w}
        </p>
      ))}
      <div className="overflow-x-auto rounded-md shadow-[inset_0_0_0_1px_var(--border-subtle)]">
        <table className="w-full min-w-[420px] text-table">
          <thead>
            <tr className="border-b border-line text-left text-meta text-fg-3">
              <th className="h-7 px-3 font-medium">Item</th>
              <th className="h-7 px-3 font-medium">Engine input</th>
              <th className="h-7 px-3 text-right font-medium">People</th>
              <th className="h-7 px-3 text-right font-medium">Emails</th>
              <th className="h-7 px-3 text-right font-medium">Columns</th>
            </tr>
          </thead>
          <tbody>
            {preview.sample.map((it, i) => {
              const company = (it.input.company ?? {}) as { domain?: string; name?: string };
              return (
                <tr key={i} className="border-b border-line/60 last:border-0">
                  <td className="max-w-40 truncate px-3 py-1.5 text-fg">{it.label}</td>
                  <td className="max-w-40 truncate px-3 py-1.5 text-fg-2">{company.domain ?? company.name ?? "—"}</td>
                  <td className="tabular px-3 py-1.5 text-right text-fg-2">{it.expected.people?.length ?? "—"}</td>
                  <td className="tabular px-3 py-1.5 text-right text-fg-2">{it.expected.emails?.length ?? 0}</td>
                  <td className="tabular px-3 py-1.5 text-right text-fg-2">{Object.keys(it.expected.enrichment ?? {}).length}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="text-micro text-fg-3">First {preview.sample.length} items shown · validated server-side.</p>
    </div>
  );
}
