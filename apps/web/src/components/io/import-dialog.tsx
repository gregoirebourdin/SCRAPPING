"use client";

import { Badge, Button, cn, Dialog, DialogContent, ProgressBar, Spinner, Switch } from "@scout/design-system";
import { useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Check, CircleAlert, FileSpreadsheet, Upload } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { invalidateRows } from "@/components/table/actions";
import { api } from "@/lib/api";
import { n } from "@/lib/format";
import { useLists } from "@/lib/queries";
import { useScope, useUI } from "@/lib/store";

interface Preview {
  headers: string[];
  sample: string[][];
  row_count: number;
  suggested_mapping: Record<string, string>;
  targets: Record<string, string>;
}

interface ImportStatus {
  id: string;
  filename: string;
  status: "pending" | "running" | "completed" | "failed";
  row_count: number;
  imported_count: number;
  merged_count: number;
  skipped_count: number;
  error_count: number;
  errors: { row?: number; message?: string }[] | null;
  list_id: string | null;
}

type Step = "pick" | "map" | "run";

/** CSV import (spec §85): column mapping, global-registry merge, "mark as previously known" (default on). */
export function ImportDialog() {
  const open = useUI((s) => s.importOpen);
  const setOpen = useUI((s) => s.setImportOpen);
  // The flow mounts fresh on every open, so its state always starts clean.
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      {open && <ImportFlow onClose={() => setOpen(false)} />}
    </Dialog>
  );
}

function ImportFlow({ onClose }: { onClose: () => void }) {
  const setOpen = (v: boolean) => !v && onClose();
  const lists = useLists();
  const qc = useQueryClient();
  const router = useRouter();
  const fileRef = useRef<HTMLInputElement>(null);
  const [step, setStep] = useState<Step>("pick");
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [listId, setListId] = useState<string>(() => useScope.getState().listId ?? "");
  const [markKnown, setMarkKnown] = useState(true);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<ImportStatus | null>(null);
  const [drag, setDrag] = useState(false);

  // Poll the import job until it settles.
  useEffect(() => {
    if (!status || status.status === "completed" || status.status === "failed") return;
    const t = setTimeout(async () => {
      try {
        setStatus(await api<ImportStatus>(`imports/${status.id}`));
      } catch {
        /* keep polling */
      }
    }, 900);
    return () => clearTimeout(t);
  }, [status]);

  useEffect(() => {
    if (status?.status === "completed") {
      invalidateRows(qc);
      toast.success(`Imported ${n(status.imported_count + status.merged_count)} leads`, {
        description: status.merged_count ? `${n(status.merged_count)} merged with existing records` : undefined,
      });
    }
  }, [status?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  async function pick(f: File) {
    if (!/\.(csv|tsv|txt)$/i.test(f.name)) {
      toast.error("Please choose a CSV file");
      return;
    }
    setFile(f);
    setBusy(true);
    try {
      const form = new FormData();
      form.append("file", f);
      const p = await api<Preview>("imports/preview", { form });
      setPreview(p);
      setMapping(Object.fromEntries(p.headers.map((h) => [h, p.suggested_mapping[h] ?? "ignore"])));
      setStep("map");
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function start() {
    if (!file) return;
    setBusy(true);
    try {
      const form = new FormData();
      form.append("file", file);
      form.append("mapping", JSON.stringify(mapping));
      if (listId) form.append("list_id", listId);
      form.append("mark_as_known", String(markKnown));
      const r = await api<{ id: string; row_count: number; status: ImportStatus["status"] }>("imports", { form });
      setStatus({
        id: r.id,
        filename: file.name,
        status: r.status,
        row_count: r.row_count,
        imported_count: 0,
        merged_count: 0,
        skipped_count: 0,
        error_count: 0,
        errors: null,
        list_id: listId || null,
      });
      setStep("run");
    } catch (e) {
      const err = e as Error & { hint?: string };
      toast.error(err.message, err.hint ? { description: err.hint } : undefined);
    } finally {
      setBusy(false);
    }
  }

  const mapped = Object.values(mapping).filter((v) => v !== "ignore");
  const hasKey = mapped.some((t) => ["full_name", "first_name", "email", "company", "website", "domain"].includes(t));
  const dupTargets = mapped.filter((t, i) => mapped.indexOf(t) !== i);
  const personLists = (lists.data ?? []).filter((l) => !l.is_archived && l.entity_type === "person");

  return (
    <DialogContent
      title="Import CSV"
      description="Imported leads join your global registry and are deduplicated against everything you already have."
      width={step === "map" ? 760 : 520}
    >
      {step === "pick" && (
        <div className="p-4">
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
              if (f) void pick(f);
            }}
            className={cn(
              "flex h-44 w-full flex-col items-center justify-center gap-2 rounded-md border border-dashed text-center transition-colors",
              drag ? "border-accent bg-accent-soft" : "border-line-strong hover:bg-surface-2",
            )}
          >
            {busy ? <Spinner size={18} /> : <Upload className="size-5 text-fg-3" />}
            <span className="text-body text-fg">{busy ? "Reading file…" : "Drop a CSV here or click to choose"}</span>
            <span className="text-meta text-fg-3">Up to 20 MB · UTF-8 or Excel CSV · comma or semicolon separated</span>
          </button>
          <input ref={fileRef} type="file" accept=".csv,.tsv,.txt,text/csv" className="hidden" onChange={(e) => e.target.files?.[0] && void pick(e.target.files[0])} />
        </div>
      )}

      {step === "map" && preview && (
        <>
          <div className="flex items-center gap-2 border-b border-line px-4 py-2 text-meta text-fg-2">
            <FileSpreadsheet className="size-4 text-fg-3" />
            <span className="font-medium text-fg">{file?.name}</span>
            <span className="text-fg-3">
              · {n(preview.row_count)} rows · {preview.headers.length} columns
            </span>
          </div>
          <div className="max-h-[46vh] overflow-y-auto">
            <table className="w-full text-body">
              <thead className="sticky top-0 bg-surface-1 text-left text-meta text-fg-3">
                <tr className="border-b border-line">
                  <th className="px-4 py-1.5 font-medium">CSV column</th>
                  <th className="w-8" />
                  <th className="px-2 py-1.5 font-medium">Scout field</th>
                  <th className="px-4 py-1.5 font-medium">Sample</th>
                </tr>
              </thead>
              <tbody>
                {preview.headers.map((h, i) => {
                  const target = mapping[h] ?? "ignore";
                  const dup = target !== "ignore" && dupTargets.includes(target);
                  return (
                    <tr key={h} className="border-b border-line/60">
                      <td className="max-w-48 truncate px-4 py-1.5 font-medium text-fg">{h}</td>
                      <td className="text-fg-3">
                        <ArrowRight className="size-3.5" />
                      </td>
                      <td className="px-2 py-1">
                        <select
                          value={target}
                          aria-label={`Map ${h}`}
                          onChange={(e) => setMapping((m) => ({ ...m, [h]: e.target.value }))}
                          className={cn(
                            "h-7 w-44 rounded-sm bg-surface-2 px-1.5 text-body outline-none shadow-[inset_0_0_0_1px_var(--border-strong)]",
                            target === "ignore" ? "text-fg-3" : "text-fg",
                            dup && "shadow-[inset_0_0_0_1px_var(--warning)]",
                          )}
                        >
                          {Object.entries(preview.targets).map(([k, label]) => (
                            <option key={k} value={k}>
                              {label}
                            </option>
                          ))}
                        </select>
                      </td>
                      <td className="max-w-64 truncate px-4 py-1.5 text-meta text-fg-3">
                        {preview.sample
                          .slice(0, 3)
                          .map((r) => r[i])
                          .filter(Boolean)
                          .join(" · ") || "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="grid gap-3 border-t border-line px-4 py-3 sm:grid-cols-2">
            <label className="flex items-center gap-2 text-body text-fg-2">
              <span className="w-24 shrink-0 text-meta text-fg-3">Add to list</span>
              <select
                value={listId}
                onChange={(e) => setListId(e.target.value)}
                className="h-7 flex-1 rounded-sm bg-surface-2 px-1.5 text-body text-fg outline-none shadow-[inset_0_0_0_1px_var(--border-strong)]"
              >
                <option value="">Don’t add to a list</option>
                {personLists.map((l) => (
                  <option key={l.id} value={l.id}>
                    {l.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex items-center justify-between gap-3 text-body text-fg-2">
              <span>
                Mark as previously known
                <span className="block text-meta text-fg-3">Excluded from future “new leads only” searches</span>
              </span>
              <Switch checked={markKnown} onCheckedChange={setMarkKnown} label="Mark as previously known" />
            </label>
          </div>
          <div className="flex items-center justify-between gap-2 border-t border-line px-4 py-3">
            <span className={cn("text-meta", hasKey && !dupTargets.length ? "text-fg-3" : "text-warning")}>
              {!hasKey ? "Map at least a name, email, company or website column" : dupTargets.length ? "Two columns map to the same field" : `${mapped.length} columns mapped`}
            </span>
            <div className="flex gap-2">
              <Button variant="ghost" onClick={() => setStep("pick")}>
                Back
              </Button>
              <Button variant="primary" disabled={!hasKey || dupTargets.length > 0 || busy} onClick={() => void start()}>
                {busy ? <Spinner size={12} className="text-accent-contrast" /> : <Upload />} Import {n(preview.row_count)} rows
              </Button>
            </div>
          </div>
        </>
      )}

      {step === "run" && status && (
        <div className="space-y-3 p-4">
          <div className="flex items-center gap-2">
            {status.status === "completed" ? (
              <Check className="size-4 text-success" />
            ) : status.status === "failed" ? (
              <CircleAlert className="size-4 text-danger" />
            ) : (
              <Spinner size={14} />
            )}
            <span className="text-body font-medium text-fg">{status.status === "completed" ? "Import complete" : status.status === "failed" ? "Import failed" : "Importing…"}</span>
            <Badge tone="muted">{status.filename}</Badge>
          </div>
          <ProgressBar
            value={status.imported_count + status.merged_count + status.skipped_count + status.error_count}
            max={Math.max(1, status.row_count)}
            tone={status.status === "completed" ? "success" : "accent"}
          />
          <dl className="grid grid-cols-4 gap-2 text-center">
            {[
              ["New", status.imported_count],
              ["Merged", status.merged_count],
              ["Skipped", status.skipped_count],
              ["Errors", status.error_count],
            ].map(([label, v]) => (
              <div key={label as string} className="rounded-sm bg-surface-2 px-2 py-1.5">
                <dt className="text-micro text-fg-3">{label}</dt>
                <dd className="tabular text-body font-medium text-fg">{n(v as number)}</dd>
              </div>
            ))}
          </dl>
          {status.errors && status.errors.length > 0 && (
            <ul className="max-h-28 space-y-0.5 overflow-y-auto text-meta text-fg-3">
              {status.errors.slice(0, 20).map((e, i) => (
                <li key={i}>
                  {e.row != null ? `Row ${e.row}: ` : ""}
                  {e.message}
                </li>
              ))}
            </ul>
          )}
          <div className="flex justify-end gap-2">
            {status.status === "completed" && status.list_id && (
              <Button
                onClick={() => {
                  setOpen(false);
                  router.push(`/lists/${status.list_id}`);
                }}
              >
                Open list
              </Button>
            )}
            <Button variant={status.status === "completed" ? "primary" : "ghost"} onClick={() => setOpen(false)}>
              {status.status === "completed" || status.status === "failed" ? "Done" : "Run in background"}
            </Button>
          </div>
        </div>
      )}
    </DialogContent>
  );
}
