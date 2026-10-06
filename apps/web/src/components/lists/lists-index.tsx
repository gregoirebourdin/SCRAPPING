"use client";

import { Badge, Button, Dialog, DialogContent, Input, Segmented } from "@scout/design-system";
import type { ListOut } from "@scout/schemas";
import { useQueryClient } from "@tanstack/react-query";
import { ListChecks, Plus } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import { DataTable, Page } from "@/components/common/page";
import { ListActions } from "@/components/lists/list-actions";
import { api } from "@/lib/api";
import { n, relTime } from "@/lib/format";
import { qk, useLists } from "@/lib/queries";

const COLORS = ["#8b8ff7", "#4fbf8b", "#d9a24a", "#62a8de", "#c486e8", "#e2826f", "#9aa0a6"];

export function ListsIndex() {
  const lists = useLists();
  const router = useRouter();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [entity, setEntity] = useState<"person" | "company">("person");
  const [color, setColor] = useState(COLORS[0]!);
  const [showArchived, setShowArchived] = useState(false);
  const rows = (lists.data ?? []).filter((l) => showArchived || !l.is_archived);

  async function create() {
    try {
      const l = await api<ListOut>("lists", { body: { name: name.trim(), entity_type: entity, color } });
      void qc.invalidateQueries({ queryKey: qk.lists });
      setOpen(false);
      setName("");
      router.push(`/lists/${l.id}`);
    } catch (e) {
      toast.error((e as Error).message);
    }
  }

  return (
    <Page
      title="Lists"
      icon={<ListChecks className="size-4 text-fg-3" />}
      actions={
        <>
          <Segmented
            value={showArchived ? "all" : "active"}
            onChange={(v) => setShowArchived(v === "all")}
            options={[
              { value: "active", label: "Active" },
              { value: "all", label: "All" },
            ]}
          />
          <Button variant="primary" onClick={() => setOpen(true)}>
            <Plus /> New list
          </Button>
        </>
      }
    >
      <DataTable<ListOut>
        rows={rows}
        loading={lists.isLoading}
        rowKey={(l) => l.id}
        onRowClick={(l) => router.push(`/lists/${l.id}`)}
        empty="No lists yet — create one, or let a search create it for you."
        columns={[
          {
            key: "name",
            label: "Name",
            render: (l) => (
              <span className="flex items-center gap-2">
                <span className="size-2 rounded-full" style={{ background: l.color ?? "var(--text-muted)" }} />
                <span className="font-medium text-fg">{l.name}</span>
                {l.is_archived && <Badge tone="muted">archived</Badge>}
              </span>
            ),
          },
          { key: "type", label: "Type", render: (l) => <span className="text-fg-2">{l.entity_type === "company" ? "Companies" : "People"}</span> },
          { key: "count", label: "Leads", className: "text-right", render: (l) => <span className="tabular text-fg">{n(l.count ?? 0)}</span> },
          { key: "source", label: "Source", render: (l) => (l.source_campaign_id ? <Badge tone="info">search</Badge> : <span className="text-fg-3">manual</span>) },
          { key: "desc", label: "Description", render: (l) => <span className="line-clamp-1 text-fg-3">{l.description ?? ""}</span> },
          { key: "updated", label: "Updated", className: "text-right", render: (l) => <span className="text-fg-3">{relTime(l.updated_at)}</span> },
          { key: "actions", label: "", className: "w-10 text-right", render: (l) => <ListActions list={l} /> },
        ]}
      />
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent title="New list" width={420}>
          <form
            className="space-y-3 px-4 py-3"
            onSubmit={(e) => {
              e.preventDefault();
              if (name.trim()) void create();
            }}
          >
            <Input autoFocus placeholder="List name" value={name} onChange={(e) => setName(e.target.value)} aria-label="List name" />
            <div className="flex items-center justify-between">
              <Segmented
                value={entity}
                onChange={setEntity}
                options={[
                  { value: "person", label: "People" },
                  { value: "company", label: "Companies" },
                ]}
              />
              <div className="flex gap-1.5" role="radiogroup" aria-label="Color">
                {COLORS.map((c) => (
                  <button
                    key={c}
                    type="button"
                    role="radio"
                    aria-checked={color === c}
                    aria-label={c}
                    onClick={() => setColor(c)}
                    className="size-4 rounded-full ring-offset-2 ring-offset-surface-1 aria-checked:ring-2 aria-checked:ring-fg-3"
                    style={{ background: c }}
                  />
                ))}
              </div>
            </div>
            <div className="flex justify-end gap-2 pt-1">
              <Button variant="ghost" onClick={() => setOpen(false)}>
                Cancel
              </Button>
              <Button type="submit" variant="primary" disabled={!name.trim()}>
                Create
              </Button>
            </div>
          </form>
        </DialogContent>
      </Dialog>
    </Page>
  );
}
