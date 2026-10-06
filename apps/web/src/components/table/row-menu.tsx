"use client";

import { ContextMenu, ContextMenuContent, ContextMenuItem, ContextMenuSeparator, ContextMenuSub, ContextMenuTrigger } from "@scout/design-system";
import type { LeadRow } from "@scout/schemas";
import { useQueryClient } from "@tanstack/react-query";
import { ArrowRightLeft, Ban, Building2, Copy, Download, ExternalLink, ListPlus, Mail, MailCheck, PanelRight, RefreshCw, Sparkles, Trash2, User } from "lucide-react";
import { useState } from "react";

import { useColumns, useLists } from "@/lib/queries";
import { NO_IDS, useUI } from "@/lib/store";

import { addToList, columnApplies, copyText, enrichColumn, exportRows, markContacted, moveToList, refreshRows, removeFromList, rowRef, suppressRows } from "./actions";
import type { TableScope } from "./use-rows";

/** Right-click row menu (spec §138, §170). If the row is part of the selection, actions apply to the whole selection. */
export function RowMenu({ row, scope, children }: { row: LeadRow; scope: TableScope; children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <ContextMenu onOpenChange={setOpen}>
      <ContextMenuTrigger asChild>{children}</ContextMenuTrigger>
      {open && <RowMenuContent row={row} scope={scope} />}
    </ContextMenu>
  );
}

function RowMenuContent({ row, scope }: { row: LeadRow; scope: TableScope }) {
  const qc = useQueryClient();
  const lists = useLists();
  const columns = useColumns(scope.listId);
  const selection = useUI((s) => s.selection[scope.key] ?? NO_IDS);
  const openDrawer = useUI((s) => s.openDrawer);
  const setSelection = useUI((s) => s.setSelection);
  const ids = selection.includes(row.id) ? selection : [row.id];
  const ref = rowRef(scope, ids);
  const many = ids.length > 1;
  const suffix = many ? ` (${ids.length})` : "";
  const clear = () => setSelection(scope.key, []);
  const targetLists = (lists.data ?? []).filter((l) => !l.is_archived && l.entity_type === scope.entityType && l.id !== scope.listId);
  const customCols = (columns.data ?? []).filter((c) => columnApplies(c, scope.entityType) && !c.is_hidden);
  const isPerson = scope.entityType === "person";

  return (
    <ContextMenuContent>
      <ContextMenuItem icon={<PanelRight />} onSelect={() => openDrawer(isPerson ? "person" : "company", row.id)} shortcut="↵">
        Open lead
      </ContextMenuItem>
      {row.website && (
        <ContextMenuItem icon={<ExternalLink />} onSelect={() => window.open(row.website!, "_blank", "noopener,noreferrer")}>
          Open website
        </ContextMenuItem>
      )}
      <ContextMenuSeparator />
      <ContextMenuSub label={`Add to list${suffix}`} icon={<ListPlus />}>
        {targetLists.length === 0 && <ContextMenuItem>No other lists yet</ContextMenuItem>}
        {targetLists.map((l) => (
          <ContextMenuItem key={l.id} onSelect={() => void addToList(qc, l, ref)}>
            <span className="flex items-center gap-2">
              <span className="size-2 rounded-full" style={{ background: l.color ?? "var(--text-muted)" }} />
              {l.name}
            </span>
          </ContextMenuItem>
        ))}
      </ContextMenuSub>
      {scope.listId && (
        <ContextMenuSub label={`Move to${suffix}`} icon={<ArrowRightLeft />}>
          {targetLists.length === 0 && <ContextMenuItem>No other lists yet</ContextMenuItem>}
          {targetLists.map((l) => (
            <ContextMenuItem key={l.id} onSelect={() => void moveToList(qc, scope.listId!, l, ref, clear)}>
              {l.name}
            </ContextMenuItem>
          ))}
        </ContextMenuSub>
      )}
      {isPerson && (
        <ContextMenuItem icon={<Mail />} onSelect={() => void refreshRows(qc, ref, "email")}>
          {row.email ? "Recheck email" : "Find email"}
          {suffix}
        </ContextMenuItem>
      )}
      <ContextMenuSub label={`Refresh${suffix}`} icon={<RefreshCw />}>
        <ContextMenuItem icon={<Building2 />} onSelect={() => void refreshRows(qc, ref, "company")}>
          Refresh company
        </ContextMenuItem>
        {isPerson && (
          <ContextMenuItem icon={<User />} onSelect={() => void refreshRows(qc, ref, "person")}>
            Refresh person
          </ContextMenuItem>
        )}
        {isPerson && (
          <ContextMenuItem icon={<MailCheck />} onSelect={() => void refreshRows(qc, ref, "email")}>
            Recheck email
          </ContextMenuItem>
        )}
        {customCols.length > 0 && <ContextMenuSeparator />}
        {customCols.map((c) => (
          <ContextMenuItem key={c.id} icon={<Sparkles />} onSelect={() => void refreshRows(qc, ref, "column", c.id)}>
            Refresh “{c.name}”
          </ContextMenuItem>
        ))}
      </ContextMenuSub>
      {customCols.length > 0 && (
        <ContextMenuSub label={`Enrich${suffix}`} icon={<Sparkles />}>
          {customCols.map((c) => (
            <ContextMenuItem key={c.id} onSelect={() => void enrichColumn(qc, c.id, c.name, ref, { onlyMissing: true, listId: scope.listId })}>
              {c.name}
            </ContextMenuItem>
          ))}
        </ContextMenuSub>
      )}
      <ContextMenuSub label="Copy" icon={<Copy />}>
        {row.email && <ContextMenuItem onSelect={() => copyText(row.email!)}>Email</ContextMenuItem>}
        {isPerson && row.full_name && <ContextMenuItem onSelect={() => copyText(row.full_name!)}>Name</ContextMenuItem>}
        {row.company && <ContextMenuItem onSelect={() => copyText(row.company!)}>Company</ContextMenuItem>}
        {row.website && <ContextMenuItem onSelect={() => copyText(row.website!)}>Website</ContextMenuItem>}
        {row.profile_url && <ContextMenuItem onSelect={() => copyText(row.profile_url!)}>Profile URL</ContextMenuItem>}
        <ContextMenuItem
          onSelect={() =>
            copyText(
              [row.full_name, row.title, row.company, row.website, row.email, row.email_status, [row.city, row.country].filter(Boolean).join(", ")]
                .filter((v) => v !== undefined)
                .map((v) => v ?? "")
                .join("\t"),
              "Row copied",
            )
          }
        >
          Row (tab-separated)
        </ContextMenuItem>
      </ContextMenuSub>
      <ContextMenuItem icon={<Download />} onSelect={() => void exportRows(scope, { ids, scopeKind: "selected" })}>
        Export{suffix}
      </ContextMenuItem>
      {isPerson && (
        <ContextMenuItem icon={<MailCheck />} onSelect={() => void Promise.all(ids.map((id) => markContacted(qc, id)))}>
          Mark contacted{suffix}
        </ContextMenuItem>
      )}
      <ContextMenuSeparator />
      <ContextMenuItem icon={<Ban />} danger onSelect={() => void suppressRows(qc, ref, "do_not_contact", clear)}>
        Suppress{suffix}
      </ContextMenuItem>
      {scope.listId && (
        <ContextMenuItem icon={<Trash2 />} danger onSelect={() => void removeFromList(qc, scope.listId!, ref, clear)}>
          Delete from list{suffix}
        </ContextMenuItem>
      )}
    </ContextMenuContent>
  );
}
