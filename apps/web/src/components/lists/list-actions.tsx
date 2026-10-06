"use client";

import { Button, Checkbox, Dialog, DialogContent, Menu, MenuContent, MenuItem, MenuSeparator, MenuTrigger } from "@scout/design-system";
import type { ListOut } from "@scout/schemas";
import { useQueryClient } from "@tanstack/react-query";
import { Archive, ArchiveRestore, MoreHorizontal, Trash2 } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import { n } from "@/lib/format";
import { qk } from "@/lib/queries";

type ListRef = Pick<ListOut, "id" | "name" | "is_archived"> & { count?: number | null };

/** Archive (hidden, everything kept, restorable) or delete permanently (optionally with the leads only in it). */
export function ListActions({ list, afterDelete }: { list: ListRef; afterDelete?: "lists" | "stay" }) {
  const qc = useQueryClient();
  const router = useRouter();
  const [confirm, setConfirm] = useState(false);
  const [purge, setPurge] = useState(true);
  const [busy, setBusy] = useState(false);

  const refresh = () => {
    void qc.invalidateQueries({ queryKey: qk.lists });
    void qc.invalidateQueries({ queryKey: ["list"] });
    void qc.invalidateQueries({ queryKey: ["rows"] });
  };

  async function setArchived(archived: boolean) {
    try {
      await api(`lists/${list.id}`, { method: "PATCH", body: { is_archived: archived } });
      toast.success(archived ? `“${list.name}” archived` : `“${list.name}” restored`, {
        description: archived ? "Hidden from your lists — every lead and its history is kept." : undefined,
      });
      refresh();
    } catch (e) {
      toast.error((e as Error).message);
    }
  }

  async function remove() {
    setBusy(true);
    try {
      const r = await api<{ people_deleted?: number; companies_deleted?: number }>(`lists/${list.id}?purge_leads=${purge}`, { method: "DELETE" });
      toast.success(`“${list.name}” deleted`, {
        description: purge
          ? `${n(r.people_deleted ?? 0)} people and ${n(r.companies_deleted ?? 0)} companies deleted · leads also in other lists were kept`
          : "The leads stay in People and Companies",
      });
      setConfirm(false);
      refresh();
      if (afterDelete === "lists") router.push("/lists");
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <span onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()} role="presentation">
      <Menu>
        <MenuTrigger asChild>
          <Button size="xs" variant="ghost" aria-label={`Actions for ${list.name}`}>
            <MoreHorizontal />
          </Button>
        </MenuTrigger>
        <MenuContent align="end">
          {list.is_archived ? (
            <MenuItem icon={<ArchiveRestore />} onSelect={() => void setArchived(false)}>
              Restore
            </MenuItem>
          ) : (
            <MenuItem icon={<Archive />} onSelect={() => void setArchived(true)}>
              Archive (keep data)
            </MenuItem>
          )}
          <MenuSeparator />
          <MenuItem icon={<Trash2 />} danger onSelect={() => setConfirm(true)}>
            Delete permanently…
          </MenuItem>
        </MenuContent>
      </Menu>
      <Dialog open={confirm} onOpenChange={setConfirm}>
        <DialogContent title={`Delete “${list.name}”?`} width={440}>
          <div className="space-y-3 px-4 py-3 text-body">
            <p className="text-fg-2">This can’t be undone. To keep everything but hide the list, archive it instead.</p>
            <label className="flex cursor-pointer items-start gap-2.5 rounded-lg bg-surface-2 p-3">
              <Checkbox checked={purge} onCheckedChange={setPurge} label="Also delete the leads" className="mt-0.5" />
              <span>
                <span className="font-medium text-fg">Also delete its leads{list.count ? ` (${n(list.count)})` : ""}</span>
                <span className="block text-meta text-fg-3">
                  People and companies that are only in this list are erased. Leads that are also in another list are kept. They may be
                  found again by future searches.
                </span>
              </span>
            </label>
            <div className="flex justify-end gap-2 pt-1">
              <Button variant="ghost" onClick={() => setConfirm(false)}>
                Cancel
              </Button>
              {!list.is_archived && (
                <Button
                  variant="ghost"
                  onClick={() => {
                    setConfirm(false);
                    void setArchived(true);
                  }}
                >
                  <Archive /> Archive instead
                </Button>
              )}
              <Button variant="danger" disabled={busy} onClick={() => void remove()}>
                <Trash2 /> Delete
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </span>
  );
}
