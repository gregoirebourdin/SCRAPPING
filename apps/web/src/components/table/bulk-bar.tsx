"use client";

import { Button, Dialog, DialogContent, Input, Kbd, Menu, MenuContent, MenuItem, MenuSeparator, MenuTrigger } from "@scout/design-system";
import type { FilterGroup, SortSpec } from "@scout/schemas";
import { useQueryClient } from "@tanstack/react-query";
import { AnimatePresence, motion } from "motion/react";
import { ArrowRightLeft, Ban, Check, Download, ListPlus, MessageSquare, Plus, RefreshCw, Sparkles, Trash2, X } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { n } from "@/lib/format";
import { useColumns, useLists } from "@/lib/queries";
import { useUI } from "@/lib/store";

import { addToList, approveRows, columnApplies, createListFrom, enrichColumn, exportRows, moveToList, refreshRows, removeFromList, rowRef, suppressRows } from "./actions";
import type { TableScope } from "./use-rows";

export function BulkBar({
  scope,
  loadedCount,
  total,
  filters,
  search,
  sort,
  visibleColumns,
}: {
  scope: TableScope;
  loadedCount: number;
  total: number | null;
  filters: FilterGroup;
  search: string;
  sort: SortSpec[];
  visibleColumns: string[];
}) {
  const qc = useQueryClient();
  const router = useRouter();
  const ids = useUI((s) => s.selection[scope.key] ?? []);
  const allMatching = useUI((s) => Boolean(s.allMatching[scope.key]));
  const setSelection = useUI((s) => s.setSelection);
  const setAllMatching = useUI((s) => s.setAllMatching);
  const setChatDraft = useUI((s) => s.setChatDraft);
  const lists = useLists();
  const columns = useColumns(scope.listId);
  const [newListOpen, setNewListOpen] = useState(false);
  const [newListName, setNewListName] = useState("");

  const count = allMatching ? (total ?? ids.length) : ids.length;
  const ref = rowRef(scope, ids, { allMatching, filters, search });
  const clear = () => setSelection(scope.key, []);
  const targetLists = (lists.data ?? []).filter((l) => !l.is_archived && l.entity_type === scope.entityType && l.id !== scope.listId);
  const customCols = (columns.data ?? []).filter((c) => columnApplies(c, scope.entityType) && !c.is_hidden);
  const canSelectAll = !allMatching && total !== null && ids.length >= loadedCount && total > ids.length && loadedCount > 0;

  return (
    <>
      <AnimatePresence>
        {ids.length > 0 && (
          <motion.div
            initial={{ opacity: 0, y: 12, x: "-50%" }}
            animate={{ opacity: 1, y: 0, x: "-50%" }}
            exit={{ opacity: 0, y: 12, x: "-50%" }}
            transition={{ duration: 0.16, ease: [0.2, 0, 0, 1] }}
            className="absolute bottom-4 left-1/2 z-30 flex max-w-[calc(100%-24px)] items-center gap-1 overflow-x-auto rounded-lg bg-surface-3 p-1 pl-3 shadow-dialog"
            role="toolbar"
            aria-label="Bulk actions"
          >
            <span className="whitespace-nowrap text-body font-medium text-fg">
              <span className="tabular">{n(count)}</span> selected
            </span>
            {canSelectAll && (
              <button type="button" onClick={() => setAllMatching(scope.key, true)} className="whitespace-nowrap px-1.5 text-meta text-accent-strong hover:underline">
                Select all {n(total)}
              </button>
            )}
            {allMatching && <span className="whitespace-nowrap px-1 text-meta text-fg-3">all matching</span>}
            <span className="mx-1 h-4 w-px bg-line-strong" />
            <Menu>
              <MenuTrigger asChild>
                <Button size="xs" variant="ghost">
                  <ListPlus /> Add to list
                </Button>
              </MenuTrigger>
              <MenuContent align="center" className="max-h-80 overflow-y-auto">
                <MenuItem icon={<Plus />} onSelect={() => setNewListOpen(true)}>
                  New list from selection…
                </MenuItem>
                {targetLists.length > 0 && <MenuSeparator />}
                {targetLists.map((l) => (
                  <MenuItem key={l.id} onSelect={() => void addToList(qc, l, ref)}>
                    <span className="flex items-center gap-2">
                      <span className="size-2 rounded-full" style={{ background: l.color ?? "var(--text-muted)" }} />
                      {l.name}
                    </span>
                  </MenuItem>
                ))}
              </MenuContent>
            </Menu>
            {scope.listId && targetLists.length > 0 && (
              <Menu>
                <MenuTrigger asChild>
                  <Button size="xs" variant="ghost">
                    <ArrowRightLeft /> Move
                  </Button>
                </MenuTrigger>
                <MenuContent align="center" className="max-h-80 overflow-y-auto">
                  {targetLists.map((l) => (
                    <MenuItem key={l.id} onSelect={() => void moveToList(qc, scope.listId!, l, ref, clear)}>
                      {l.name}
                    </MenuItem>
                  ))}
                </MenuContent>
              </Menu>
            )}
            <Menu>
              <MenuTrigger asChild>
                <Button size="xs" variant="ghost">
                  <Sparkles /> Enrich
                </Button>
              </MenuTrigger>
              <MenuContent align="center">
                {scope.entityType === "person" && <MenuItem onSelect={() => void refreshRows(qc, ref, "email")}>Find / recheck emails</MenuItem>}
                {customCols.map((c) => (
                  <MenuItem key={c.id} onSelect={() => void enrichColumn(qc, c.id, c.name, ref, { onlyMissing: true, listId: scope.listId })}>
                    {c.name}
                  </MenuItem>
                ))}
                <MenuSeparator />
                <MenuItem icon={<MessageSquare />} onSelect={() => setChatDraft(`Add a column for the ${n(count)} selected leads: `)}>
                  New AI column…
                </MenuItem>
              </MenuContent>
            </Menu>
            <Menu>
              <MenuTrigger asChild>
                <Button size="xs" variant="ghost">
                  <RefreshCw /> Refresh
                </Button>
              </MenuTrigger>
              <MenuContent align="center">
                <MenuItem onSelect={() => void refreshRows(qc, ref, "company")}>Refresh companies</MenuItem>
                {scope.entityType === "person" && <MenuItem onSelect={() => void refreshRows(qc, ref, "person")}>Refresh people</MenuItem>}
                {scope.entityType === "person" && <MenuItem onSelect={() => void refreshRows(qc, ref, "email")}>Recheck emails</MenuItem>}
              </MenuContent>
            </Menu>
            <Menu>
              <MenuTrigger asChild>
                <Button size="xs" variant="ghost">
                  <Download /> Export
                </Button>
              </MenuTrigger>
              <MenuContent align="center">
                <MenuItem
                  onSelect={() =>
                    void exportRows(
                      scope,
                      allMatching ? { scopeKind: "view", filters, search, sort, columns: visibleColumns } : { scopeKind: "selected", ids, sort, columns: visibleColumns },
                    )
                  }
                >
                  CSV — visible columns
                </MenuItem>
                <MenuItem onSelect={() => void exportRows(scope, allMatching ? { scopeKind: "view", filters, search, sort } : { scopeKind: "selected", ids, sort })}>
                  CSV — all fields
                </MenuItem>
                <MenuItem
                  onSelect={() =>
                    void exportRows(scope, allMatching ? { scopeKind: "view", filters, search, sort, format: "json" } : { scopeKind: "selected", ids, sort, format: "json" })
                  }
                >
                  JSON
                </MenuItem>
              </MenuContent>
            </Menu>
            {scope.kind === "review" && (
              <Button size="xs" variant="ghost" onClick={() => void approveRows(qc, scope.entityType, ids, clear)}>
                <Check /> Approve
              </Button>
            )}
            <Button size="xs" variant="ghost" onClick={() => setChatDraft(`For the ${n(count)} selected leads, `)} title="Ask the AI about the selection">
              <MessageSquare /> Ask AI
            </Button>
            <span className="mx-1 h-4 w-px bg-line-strong" />
            <Button
              size="xs"
              variant="ghost"
              className="text-fg-3 hover:text-danger"
              onClick={() => void suppressRows(qc, ref, "do_not_contact", clear)}
              title="Never export or rediscover"
            >
              <Ban /> Suppress
            </Button>
            {scope.listId && (
              <Button size="xs" variant="ghost" className="text-fg-3 hover:text-danger" onClick={() => void removeFromList(qc, scope.listId!, ref, clear)}>
                <Trash2 /> Remove
              </Button>
            )}
            <button
              type="button"
              onClick={clear}
              className="ml-0.5 flex h-6 items-center gap-1 rounded-sm px-1.5 text-fg-3 hover:bg-surface-2 hover:text-fg"
              aria-label="Clear selection"
            >
              <Kbd>Esc</Kbd>
              <X className="size-3.5" />
            </button>
          </motion.div>
        )}
      </AnimatePresence>
      <Dialog open={newListOpen} onOpenChange={setNewListOpen}>
        <DialogContent title="New list from selection" description={`${n(count)} leads will be added`} width={400}>
          <form
            className="space-y-3 px-4 py-3"
            onSubmit={async (e) => {
              e.preventDefault();
              if (!newListName.trim()) return;
              setNewListOpen(false);
              const l = await createListFrom(qc, newListName.trim(), ref);
              setNewListName("");
              if (l) {
                clear();
                router.push(`/lists/${l.id}`);
              }
            }}
          >
            <Input autoFocus placeholder="e.g. Priority ManyChat" value={newListName} onChange={(e) => setNewListName(e.target.value)} aria-label="List name" />
            <div className="flex justify-end gap-2">
              <Button variant="ghost" onClick={() => setNewListOpen(false)}>
                Cancel
              </Button>
              <Button type="submit" variant="primary" disabled={!newListName.trim()}>
                Create list
              </Button>
            </div>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}
