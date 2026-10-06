"use client";

import { Button, Dialog, DialogContent, Input, Menu, MenuContent, MenuItem, MenuSeparator, MenuTrigger } from "@scout/design-system";
import type { SortSpec } from "@scout/schemas";
import { useQueryClient } from "@tanstack/react-query";
import { ArrowDown, ArrowUp, Copy, EyeOff, Filter, Pencil, Pin, PinOff, RefreshCw, Settings2, Sparkles, Trash2, X } from "lucide-react";
import { useRef, useState } from "react";
import { toast } from "sonner";

import { api } from "@/lib/api";

import { enrichColumn, invalidateRows } from "./actions";
import type { ColumnSpec } from "./cells";
import type { TableScope } from "./use-rows";

/** Column header menu (spec §122): sort, filter, pin, hide; custom columns add rename, duplicate,
 *  enrich missing, refresh, configuration and delete. */
export function HeaderMenu({
  spec,
  sort,
  isPinned,
  onSort,
  onFilter,
  onPin,
  onHide,
  onConfigure,
  scope,
  children,
}: {
  spec: ColumnSpec;
  sort?: SortSpec;
  isPinned: boolean;
  onSort: (dir: "asc" | "desc" | null) => void;
  onFilter?: () => void;
  onPin: (v: boolean) => void;
  onHide: () => void;
  onConfigure?: () => void;
  scope: TableScope;
  children: React.ReactNode;
}) {
  const qc = useQueryClient();
  const [renaming, setRenaming] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  // "Filter…" opens the table's filter editor: the closing menu must not take the focus back (it would dismiss it).
  const keepFocus = useRef(false);
  const col = spec.custom;

  async function call(fn: () => Promise<unknown>, ok?: string) {
    try {
      await fn();
      if (ok) toast(ok);
      void qc.invalidateQueries({ queryKey: ["columns"] });
      void qc.invalidateQueries({ queryKey: ["fields"] });
      invalidateRows(qc);
    } catch (e) {
      toast.error((e as Error).message);
    }
  }

  return (
    <>
      <Menu>
        <MenuTrigger asChild>
          <button type="button" className="flex h-full min-w-0 flex-1 items-center text-left outline-none focus-visible:shadow-focus" aria-label={`${spec.label} column menu`}>
            {children}
          </button>
        </MenuTrigger>
        <MenuContent
          className="min-w-[200px]"
          onCloseAutoFocus={(e) => {
            if (keepFocus.current) e.preventDefault();
            keepFocus.current = false;
          }}
        >
          {spec.sortField && (
            <>
              <MenuItem icon={<ArrowUp />} onSelect={() => onSort(sort?.direction === "asc" ? null : "asc")}>
                Sort ascending{sort?.direction === "asc" ? " ✓" : ""}
              </MenuItem>
              <MenuItem icon={<ArrowDown />} onSelect={() => onSort(sort?.direction === "desc" ? null : "desc")}>
                Sort descending{sort?.direction === "desc" ? " ✓" : ""}
              </MenuItem>
              {sort && (
                <MenuItem icon={<X />} onSelect={() => onSort(null)}>
                  Clear sort
                </MenuItem>
              )}
            </>
          )}
          {onFilter && (
            <MenuItem
              icon={<Filter />}
              onSelect={() => {
                keepFocus.current = true;
                onFilter();
              }}
            >
              Filter…
            </MenuItem>
          )}
          <MenuSeparator />
          <MenuItem icon={isPinned ? <PinOff /> : <Pin />} onSelect={() => onPin(!isPinned)}>
            {isPinned ? "Unpin" : "Pin"}
          </MenuItem>
          <MenuItem icon={<EyeOff />} onSelect={onHide}>
            Hide
          </MenuItem>
          {col && (
            <>
              <MenuSeparator />
              <MenuItem icon={<Pencil />} onSelect={() => setRenaming(true)}>
                Rename
              </MenuItem>
              <MenuItem icon={<Copy />} onSelect={() => void call(() => api(`columns/${col.id}/duplicate`, { method: "POST" }), `Duplicated ${col.name}`)}>
                Duplicate
              </MenuItem>
              <MenuItem icon={<Sparkles />} onSelect={() => void enrichColumn(qc, col.id, col.name, null, { onlyMissing: true, listId: scope.listId })}>
                Enrich missing
              </MenuItem>
              <MenuItem icon={<RefreshCw />} onSelect={() => void enrichColumn(qc, col.id, col.name, null, { force: true, onlyMissing: false, listId: scope.listId })}>
                Refresh all
              </MenuItem>
              {onConfigure && (
                <MenuItem icon={<Settings2 />} onSelect={onConfigure}>
                  View configuration
                </MenuItem>
              )}
              <MenuSeparator />
              <MenuItem icon={<Trash2 />} danger onSelect={() => setConfirmDelete(true)}>
                Delete column
              </MenuItem>
            </>
          )}
        </MenuContent>
      </Menu>
      {col && (
        <RenameDialog
          open={renaming}
          onOpenChange={setRenaming}
          initial={col.name}
          onSave={(name) => call(() => api(`columns/${col.id}`, { method: "PATCH", body: { name } }), "Column renamed")}
        />
      )}
      {col && (
        <Dialog open={confirmDelete} onOpenChange={setConfirmDelete}>
          <DialogContent title={`Delete “${col.name}”?`} description="The column and its values are removed from this list. You can undo from Activity." width={420}>
            <div className="flex justify-end gap-2 px-4 py-3">
              <Button variant="ghost" onClick={() => setConfirmDelete(false)}>
                Cancel
              </Button>
              <Button
                variant="danger"
                onClick={() => {
                  setConfirmDelete(false);
                  void call(() => api(`columns/${col.id}`, { method: "DELETE" }), `Deleted ${col.name}`);
                }}
              >
                <Trash2 /> Delete column
              </Button>
            </div>
          </DialogContent>
        </Dialog>
      )}
    </>
  );
}

function RenameDialog({ open, onOpenChange, initial, onSave }: { open: boolean; onOpenChange: (v: boolean) => void; initial: string; onSave: (name: string) => void }) {
  const [name, setName] = useState(initial);
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent title="Rename column" width={380}>
        <form
          className="space-y-3 px-4 py-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (name.trim()) onSave(name.trim());
            onOpenChange(false);
          }}
        >
          <Input autoFocus value={name} onChange={(e) => setName(e.target.value)} aria-label="Column name" />
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" variant="primary">
              Save
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
