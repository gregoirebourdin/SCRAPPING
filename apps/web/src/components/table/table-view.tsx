"use client";

import {
  Badge,
  Button,
  cn,
  Dialog,
  DialogContent,
  IconButton,
  Input,
  Menu,
  MenuCheckboxItem,
  MenuContent,
  MenuItem,
  MenuLabel,
  MenuSeparator,
  MenuTrigger,
  Popover,
  PopoverContent,
  PopoverTrigger,
  ProgressBar,
  Segmented,
  Tip,
} from "@scout/design-system";
import type { FilterCondition, FilterGroup, SortSpec, ViewOut } from "@scout/schemas";
import { useQueryClient } from "@tanstack/react-query";
import { ArrowDownUp, Columns3, Copy, Download, Filter, MoreHorizontal, Pencil, Plus, RotateCcw, Save, Search, Sparkles, Trash2, Upload, X } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { useLive } from "@/components/shell/live-events";
import { api } from "@/lib/api";
import { n, pct } from "@/lib/format";
import { qk, useCampaign, useList, useViews } from "@/lib/queries";
import { defaultLayout, EMPTY_FILTERS, type TableLayout, useScope, useUI } from "@/lib/store";
import { TABLET_UP, useMediaQuery } from "@/lib/use-media";

import { columnApplies, exportRows, invalidateRows } from "./actions";
import { BulkBar } from "./bulk-bar";
import { type ColumnSpec, companyColumns, customColumnSpec, personColumns } from "./cells";
import { AddColumnDialog, ColumnConfigDialog } from "./column-dialogs";
import { FilterBuilder, opLabel } from "./filter-builder";
import { LeadTable } from "./lead-table";
import { MobileRows } from "./mobile-rows";
import { type TableScope, useFields, useRows } from "./use-rows";

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

export function TableView({
  scope,
  title,
  subtitle,
  icon,
  emptyState,
  headerExtra,
}: {
  scope: TableScope;
  title: React.ReactNode;
  subtitle?: React.ReactNode;
  icon?: React.ReactNode;
  emptyState?: React.ReactNode;
  headerExtra?: React.ReactNode;
}) {
  const qc = useQueryClient();
  const stored = useUI((s) => s.layouts[scope.key]);
  const layout: TableLayout = stored ?? defaultLayout();
  const patch = (p: Partial<TableLayout>) => useUI.getState().patchLayout(scope.key, p);
  const setImportOpen = useUI((s) => s.setImportOpen);
  const [filterOpen, setFilterOpen] = useState(false);
  const [addColOpen, setAddColOpen] = useState(false);
  const [configCol, setConfigCol] = useState<string | null>(null);

  const tabletUp = useMediaQuery(TABLET_UP);
  const search = useDebounced(layout.search, 250);
  const debouncedFilters = useDebounced(layout.filters, 300);
  const fields = useFields(scope.entityType, scope.listId);
  const q = useRows(scope, debouncedFilters, layout.sort, search);
  const pages = useMemo(() => q.data?.pages ?? [], [q.data]);
  const rows = useMemo(() => pages.flatMap((p) => p.rows), [pages]);
  const total = pages[0]?.total ?? null;
  const serverColumns = pages[0]?.columns;

  const specs = useMemo<ColumnSpec[]>(() => {
    const base = scope.entityType === "person" ? personColumns() : companyColumns();
    const custom = (serverColumns ?? []).filter((c) => !c.is_hidden && columnApplies(c, scope.entityType)).map(customColumnSpec);
    return [...base, ...custom];
  }, [scope.entityType, serverColumns]);

  const visibleColumns = useMemo(() => specs.filter((s) => layout.columnVisibility[s.id] !== false).map((s) => s.id), [specs, layout.columnVisibility]);

  /* Publish the on-screen scope for the chat operator (spec §35). */
  const list = useList(scope.listId);
  useEffect(() => {
    useScope.getState().set({
      scope: scope.key,
      listId: scope.listId,
      listName: scope.listId ? (list.data?.name ?? null) : null,
      entityType: scope.entityType,
      campaignId: scope.campaignId,
      rowCount: total,
      visibleColumns,
      viewName: null,
    });
  }, [scope.key, scope.listId, scope.entityType, scope.campaignId, list.data?.name, total, visibleColumns]);

  const conditions = (layout.filters.conditions ?? []).filter((c): c is FilterCondition => "field" in c);
  const fieldByKey = useMemo(() => new Map((fields.data ?? []).map((f) => [f.key, f])), [fields.data]);

  function addFilter(field: string) {
    const f = fieldByKey.get(field);
    const op: FilterCondition["operator"] =
      f?.type === "number" || f?.type === "percent" ? "gte" : f?.type === "enum" ? "eq" : f?.type === "boolean" ? "is_true" : f?.type === "date" ? "after" : "contains";
    patch({ filters: { op: layout.filters.op ?? "and", conditions: [...conditions, { field, operator: op, value: null }] } });
    setFilterOpen(true);
  }

  const newLeads = useLive((s) => (scope.listId ? (s.newLeads[scope.listId] ?? 0) : 0));
  const clearNew = useLive((s) => s.clearNewLeads);
  const activeFilterCount = conditions.filter(
    (c) => (c.value !== null && c.value !== "") || ["is_empty", "not_empty", "is_true", "is_false", "is_unknown"].includes(c.operator),
  ).length;

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      {/* ---------- top bar (spec §121) ---------- */}
      <header className="flex h-12 shrink-0 items-center gap-2 border-b border-line px-3">
        <div className="flex min-w-0 items-center gap-2">
          {icon}
          <h1 className="truncate text-heading text-fg">{title}</h1>
          <span className="tabular shrink-0 text-meta text-fg-3">{total === null ? "" : n(total)}</span>
          {subtitle}
        </div>
        {headerExtra}
        <div className="ml-auto hidden items-center gap-1 md:flex">
          <div className="relative">
            <Search className="pointer-events-none absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-fg-3" />
            <Input
              value={layout.search}
              onChange={(e) => patch({ search: e.target.value })}
              placeholder="Search"
              aria-label="Search rows"
              className="h-7 w-44 pl-7 transition-[width] focus:w-60"
            />
            {layout.search && (
              <button type="button" onClick={() => patch({ search: "" })} className="absolute right-1.5 top-1/2 -translate-y-1/2 text-fg-3 hover:text-fg" aria-label="Clear search">
                <X className="size-3" />
              </button>
            )}
          </div>
          <Popover open={filterOpen} onOpenChange={setFilterOpen}>
            <PopoverTrigger asChild>
              <Button size="sm" variant={activeFilterCount ? "subtle" : "ghost"}>
                <Filter /> Filter{activeFilterCount ? <span className="tabular">· {activeFilterCount}</span> : null}
              </Button>
            </PopoverTrigger>
            <PopoverContent align="end" className="p-2">
              <FilterBuilder fields={fields.data ?? []} value={layout.filters} onChange={(g) => patch({ filters: g })} />
            </PopoverContent>
          </Popover>
          <SortMenu sort={layout.sort} specs={specs} onChange={(sort) => patch({ sort })} />
          <ColumnsMenu specs={specs} layout={layout} onPatch={patch} />
          <Tip content="Add an AI column">
            <Button size="sm" variant="ghost" onClick={() => setAddColOpen(true)}>
              <Sparkles /> Enrich
            </Button>
          </Tip>
          <Tip content="Import CSV">
            <IconButton label="Import CSV" onClick={() => setImportOpen(true)}>
              <Upload className="size-3.5" />
            </IconButton>
          </Tip>
          <Menu>
            <MenuTrigger asChild>
              <IconButton label="Export">
                <Download className="size-3.5" />
              </IconButton>
            </MenuTrigger>
            <MenuContent align="end">
              <MenuLabel>Export CSV</MenuLabel>
              <MenuItem onSelect={() => void exportRows(scope, { scopeKind: "view", filters: layout.filters, search: layout.search, sort: layout.sort, columns: visibleColumns })}>
                Current view ({n(total)})
              </MenuItem>
              {scope.listId && <MenuItem onSelect={() => void exportRows(scope, { scopeKind: "list" })}>Entire list</MenuItem>}
              <MenuItem onSelect={() => void exportRows(scope, { scopeKind: "view", filters: layout.filters, search: layout.search, sort: layout.sort })}>
                Current view — all fields
              </MenuItem>
              <MenuSeparator />
              <MenuItem onSelect={() => void exportRows(scope, { scopeKind: "view", filters: layout.filters, search: layout.search, sort: layout.sort, format: "json" })}>
                JSON
              </MenuItem>
            </MenuContent>
          </Menu>
          <MoreMenu scope={scope} layout={layout} onPatch={patch} />
        </div>
      </header>

      {!tabletUp && (
        <div className="relative shrink-0 border-b border-line px-3 py-2">
          <Search className="pointer-events-none absolute left-5 top-1/2 size-3.5 -translate-y-1/2 text-fg-3" />
          <Input value={layout.search} onChange={(e) => patch({ search: e.target.value })} placeholder="Search" aria-label="Search rows" className="h-8 pl-7" />
        </div>
      )}

      <ViewTabs scope={scope} layout={layout} onPatch={patch} />

      {/* ---------- active filters ---------- */}
      {(conditions.length > 0 || layout.sort.length > 0) && (
        <div className="flex shrink-0 flex-wrap items-center gap-1.5 border-b border-line px-3 py-1.5">
          {conditions.map((c, i) => {
            const f = fieldByKey.get(c.field);
            const value = Array.isArray(c.value) ? (c.value as unknown[]).join(c.operator === "between" ? " – " : ", ") : c.value == null ? "" : String(c.value);
            return (
              <span key={i} className="inline-flex h-6 items-center gap-1 rounded-sm bg-surface-2 pl-2 pr-1 text-meta shadow-[inset_0_0_0_1px_var(--border-subtle)]">
                <button type="button" onClick={() => setFilterOpen(true)} className="flex items-center gap-1">
                  <span className="text-fg-2">{f?.label ?? c.field}</span>
                  <span className="text-fg-3">{opLabel(f, c.operator)}</span>
                  {value && <span className="max-w-40 truncate font-medium text-fg">{value.replace(/_/g, " ")}</span>}
                </button>
                <button
                  type="button"
                  onClick={() => patch({ filters: { op: layout.filters.op ?? "and", conditions: conditions.filter((_, j) => j !== i) } })}
                  className="rounded-xs p-0.5 text-fg-3 hover:bg-surface-3 hover:text-fg"
                  aria-label="Remove filter"
                >
                  <X className="size-3" />
                </button>
              </span>
            );
          })}
          {layout.sort.map((s) => (
            <span key={s.field} className="inline-flex h-6 items-center gap-1 rounded-sm bg-surface-2 pl-2 pr-1 text-meta text-fg-2 shadow-[inset_0_0_0_1px_var(--border-subtle)]">
              <ArrowDownUp className="size-3 text-fg-3" />
              {specs.find((sp) => sp.sortField === s.field)?.label ?? s.field} {s.direction === "asc" ? "↑" : "↓"}
              <button
                type="button"
                onClick={() => patch({ sort: layout.sort.filter((x) => x.field !== s.field) })}
                className="rounded-xs p-0.5 text-fg-3 hover:bg-surface-3 hover:text-fg"
                aria-label="Remove sort"
              >
                <X className="size-3" />
              </button>
            </span>
          ))}
          <button type="button" onClick={() => patch({ filters: { ...EMPTY_FILTERS, conditions: [] }, sort: [] })} className="px-1 text-meta text-fg-3 hover:text-fg">
            Clear
          </button>
        </div>
      )}

      {scope.listId && <QualityStrip listId={scope.listId} />}

      {/* ---------- table ---------- */}
      <div className="relative flex min-h-0 flex-1 flex-col">
        {newLeads > 0 && (
          <button
            type="button"
            onClick={() => {
              clearNew(scope.listId!);
              invalidateRows(qc);
            }}
            className="absolute left-1/2 top-10 z-30 -translate-x-1/2 animate-fade-in rounded-full bg-accent px-3 py-1 text-meta font-medium text-accent-contrast shadow-popover hover:bg-accent-strong"
          >
            +{n(newLeads)} new lead{newLeads === 1 ? "" : "s"}
          </button>
        )}
        {tabletUp ? (
          <>
            <LeadTable
              scope={scope}
              specs={specs}
              rows={rows}
              total={total}
              loading={q.isLoading}
              fetchingMore={q.isFetchingNextPage}
              hasMore={Boolean(q.hasNextPage)}
              onLoadMore={() => void q.fetchNextPage()}
              fields={fields.data ?? []}
              sort={layout.sort}
              onSort={(sort) => patch({ sort })}
              onAddFilter={addFilter}
              onAddColumn={() => setAddColOpen(true)}
              onConfigureColumn={setConfigCol}
              emptyState={
                q.isError ? (
                  <div className="max-w-sm text-center">
                    <p className="text-body text-fg">Could not load rows</p>
                    <p className="mt-1 text-meta text-fg-3">{(q.error as Error).message}</p>
                    <Button size="sm" className="mt-3" onClick={() => void q.refetch()}>
                      <RotateCcw /> Retry
                    </Button>
                  </div>
                ) : conditions.length || layout.search ? (
                  <div className="text-center">
                    <p className="text-body text-fg">No rows match</p>
                    <p className="mt-1 text-meta text-fg-3">Try removing a filter or clearing the search.</p>
                    <Button size="sm" variant="ghost" className="mt-3" onClick={() => patch({ filters: { ...EMPTY_FILTERS, conditions: [] }, search: "" })}>
                      Clear filters
                    </Button>
                  </div>
                ) : (
                  emptyState
                )
              }
            />
            <BulkBar scope={scope} loadedCount={rows.length} total={total} filters={debouncedFilters} search={search} sort={layout.sort} visibleColumns={visibleColumns} />
          </>
        ) : (
          <MobileRows
            rows={rows}
            entityType={scope.entityType}
            loading={q.isLoading}
            hasMore={Boolean(q.hasNextPage)}
            fetchingMore={q.isFetchingNextPage}
            onLoadMore={() => void q.fetchNextPage()}
            emptyState={emptyState}
          />
        )}
      </div>

      <AddColumnDialog open={addColOpen} onOpenChange={setAddColOpen} listId={scope.listId} entityType={scope.entityType} />
      <ColumnConfigDialog columnId={configCol} listId={scope.listId} onOpenChange={(v) => !v && setConfigCol(null)} />
    </div>
  );
}

/* ------------------------------------------------------------------------------------------------ */

function SortMenu({ sort, specs, onChange }: { sort: SortSpec[]; specs: ColumnSpec[]; onChange: (s: SortSpec[]) => void }) {
  const sortable = specs.filter((s) => s.sortField);
  const current = sort[0];
  return (
    <Menu>
      <MenuTrigger asChild>
        <Button size="sm" variant={sort.length ? "subtle" : "ghost"}>
          <ArrowDownUp /> Sort
        </Button>
      </MenuTrigger>
      <MenuContent align="end" className="max-h-96 overflow-y-auto">
        {current && (
          <>
            <MenuItem icon={<X />} onSelect={() => onChange([])}>
              Clear sort
            </MenuItem>
            <MenuItem icon={<ArrowDownUp />} onSelect={() => onChange([{ field: current.field, direction: current.direction === "asc" ? "desc" : "asc" }])}>
              Reverse direction
            </MenuItem>
            <MenuSeparator />
          </>
        )}
        {sortable.map((s) => (
          <MenuItem
            key={s.id}
            onSelect={() =>
              onChange([{ field: s.sortField!, direction: s.sortField === "icp_score" || s.sortField === "updated_at" || s.sortField?.includes("confidence") ? "desc" : "asc" }])
            }
          >
            <span className={cn(current?.field === s.sortField && "text-accent-strong")}>{s.label}</span>
          </MenuItem>
        ))}
      </MenuContent>
    </Menu>
  );
}

function ColumnsMenu({ specs, layout, onPatch }: { specs: ColumnSpec[]; layout: TableLayout; onPatch: (p: Partial<TableLayout>) => void }) {
  return (
    <Menu>
      <MenuTrigger asChild>
        <Button size="sm" variant="ghost">
          <Columns3 /> Columns
        </Button>
      </MenuTrigger>
      <MenuContent align="end" className="max-h-[420px] w-60 overflow-y-auto">
        <div className="flex items-center justify-between px-2 py-1.5">
          <span className="text-meta text-fg-3">Density</span>
          <Segmented
            value={layout.density}
            onChange={(density) => onPatch({ density })}
            options={[
              { value: "compact", label: "Compact" },
              { value: "comfortable", label: "Comfortable" },
            ]}
          />
        </div>
        <MenuSeparator />
        <MenuLabel>Columns</MenuLabel>
        {specs.map((s) => (
          <MenuCheckboxItem
            key={s.id}
            checked={layout.columnVisibility[s.id] !== false}
            onCheckedChange={(v) => onPatch({ columnVisibility: { ...layout.columnVisibility, [s.id]: v } })}
          >
            <span className="flex flex-1 items-center gap-1.5 truncate">
              {s.custom && <span className="size-1.5 rounded-full bg-accent/70" />}
              {s.label}
            </span>
          </MenuCheckboxItem>
        ))}
        <MenuSeparator />
        <MenuItem icon={<RotateCcw />} onSelect={() => onPatch({ columnOrder: [], columnSizing: {}, columnVisibility: {}, pinned: defaultLayout().pinned })}>
          Reset layout
        </MenuItem>
      </MenuContent>
    </Menu>
  );
}

function MoreMenu({ scope, layout, onPatch }: { scope: TableScope; layout: TableLayout; onPatch: (p: Partial<TableLayout>) => void }) {
  const qc = useQueryClient();
  const router = useRouter();
  const list = useList(scope.listId);
  const [renameOpen, setRenameOpen] = useState(false);
  const [name, setName] = useState("");
  const [confirmArchive, setConfirmArchive] = useState(false);

  async function call(fn: () => Promise<unknown>, ok: string) {
    try {
      await fn();
      toast(ok);
      void qc.invalidateQueries({ queryKey: qk.lists });
      void qc.invalidateQueries({ queryKey: ["list"] });
    } catch (e) {
      toast.error((e as Error).message);
    }
  }

  return (
    <>
      <Menu>
        <MenuTrigger asChild>
          <IconButton label="More">
            <MoreHorizontal className="size-4" />
          </IconButton>
        </MenuTrigger>
        <MenuContent align="end">
          <MenuItem onSelect={() => onPatch({ density: layout.density === "compact" ? "comfortable" : "compact" })}>
            {layout.density === "compact" ? "Comfortable rows" : "Compact rows"}
          </MenuItem>
          {scope.listId && list.data && (
            <>
              <MenuSeparator />
              <MenuItem
                icon={<Pencil />}
                onSelect={() => {
                  setName(list.data!.name);
                  setRenameOpen(true);
                }}
              >
                Rename list
              </MenuItem>
              <MenuItem
                icon={<Copy />}
                onSelect={async () => {
                  try {
                    const l = await api<{ id: string; name: string }>(`lists/${scope.listId}/duplicate`, { method: "POST" });
                    toast(`Duplicated as ${l.name}`);
                    void qc.invalidateQueries({ queryKey: qk.lists });
                    router.push(`/lists/${l.id}`);
                  } catch (e) {
                    toast.error((e as Error).message);
                  }
                }}
              >
                Duplicate list
              </MenuItem>
              <MenuItem icon={<Trash2 />} danger onSelect={() => setConfirmArchive(true)}>
                Archive list
              </MenuItem>
            </>
          )}
        </MenuContent>
      </Menu>
      <Dialog open={renameOpen} onOpenChange={setRenameOpen}>
        <DialogContent title="Rename list" width={380}>
          <form
            className="space-y-3 px-4 py-3"
            onSubmit={(e) => {
              e.preventDefault();
              setRenameOpen(false);
              if (name.trim()) void call(() => api(`lists/${scope.listId}`, { method: "PATCH", body: { name: name.trim() } }), "List renamed");
            }}
          >
            <Input autoFocus value={name} onChange={(e) => setName(e.target.value)} aria-label="List name" />
            <div className="flex justify-end gap-2">
              <Button variant="ghost" onClick={() => setRenameOpen(false)}>
                Cancel
              </Button>
              <Button type="submit" variant="primary">
                Save
              </Button>
            </div>
          </form>
        </DialogContent>
      </Dialog>
      <Dialog open={confirmArchive} onOpenChange={setConfirmArchive}>
        <DialogContent title={`Archive “${list.data?.name ?? "list"}”?`} description="Leads stay in your database and keep counting as already discovered." width={420}>
          <div className="flex justify-end gap-2 px-4 py-3">
            <Button variant="ghost" onClick={() => setConfirmArchive(false)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              onClick={async () => {
                setConfirmArchive(false);
                await call(() => api(`lists/${scope.listId}`, { method: "PATCH", body: { is_archived: true } }), "List archived");
                router.push("/people");
              }}
            >
              Archive
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}

/* ---- saved views (spec §37 saved views) ---------------------------------------------------------- */

function layoutFromView(v: ViewOut): Partial<TableLayout> {
  return {
    filters: (v.filters as FilterGroup) ?? { ...EMPTY_FILTERS, conditions: [] },
    sort: (v.sort as SortSpec[]) ?? [],
    columnOrder: (v.column_order as string[]) ?? [],
    columnVisibility: (v.column_visibility as Record<string, boolean>) ?? {},
    columnSizing: (v.column_widths as Record<string, number>) ?? {},
    pinned: v.pinned_columns?.length ? (v.pinned_columns as string[]) : defaultLayout().pinned,
    density: (v.density as TableLayout["density"]) ?? "compact",
    viewId: v.id,
  };
}

function viewBody(layout: TableLayout) {
  return {
    filters: layout.filters,
    sort: layout.sort,
    column_order: layout.columnOrder,
    column_visibility: layout.columnVisibility,
    column_widths: layout.columnSizing,
    pinned_columns: layout.pinned,
    density: layout.density,
  };
}

function ViewTabs({ scope, layout, onPatch }: { scope: TableScope; layout: TableLayout; onPatch: (p: Partial<TableLayout>) => void }) {
  const qc = useQueryClient();
  const views = useViews(scope.listId, scope.entityType);
  const [saveOpen, setSaveOpen] = useState(false);
  const [name, setName] = useState("");
  const viewsKey = qk.views(scope.listId ? `list:${scope.listId}` : scope.entityType);
  const defaultView = (views.data ?? []).find((v) => v.is_default) ?? null;
  const otherViews = (views.data ?? []).filter((v) => v.id !== defaultView?.id);
  const current = (views.data ?? []).find((v) => v.id === layout.viewId) ?? null;
  const dirty = current
    ? JSON.stringify(viewBody({ ...defaultLayout(), ...layoutFromView(current) } as TableLayout)) !== JSON.stringify(viewBody(layout))
    : layout.filters.conditions?.length || layout.sort.length;

  if (scope.kind === "campaign" || scope.kind === "review") return null;

  async function saveNew() {
    try {
      const v = await api<ViewOut>("views", { body: { name: name.trim(), list_id: scope.listId, entity_type: scope.entityType, ...viewBody(layout) } });
      onPatch({ viewId: v.id });
      void qc.invalidateQueries({ queryKey: viewsKey });
      toast(`Saved view “${v.name}”`);
    } catch (e) {
      toast.error((e as Error).message);
    }
  }

  async function update(v: ViewOut) {
    try {
      await api(`views/${v.id}`, { method: "PATCH", body: viewBody(layout) });
      void qc.invalidateQueries({ queryKey: viewsKey });
      toast(`Updated “${v.name}”`);
    } catch (e) {
      toast.error((e as Error).message);
    }
  }

  async function remove(v: ViewOut) {
    try {
      await api(`views/${v.id}`, { method: "DELETE" });
      if (layout.viewId === v.id) onPatch({ viewId: null });
      void qc.invalidateQueries({ queryKey: viewsKey });
    } catch (e) {
      toast.error((e as Error).message);
    }
  }

  return (
    <div className="flex h-9 shrink-0 items-center gap-0.5 overflow-x-auto border-b border-line px-2">
      <ViewTab
        active={!layout.viewId || layout.viewId === defaultView?.id}
        onClick={() => onPatch(defaultView ? layoutFromView(defaultView) : { ...defaultLayout(), search: layout.search, density: layout.density })}
      >
        {defaultView?.name ?? "All"}
      </ViewTab>
      {otherViews.map((v) => (
        <div key={v.id} className="group/tab relative flex items-center">
          <ViewTab active={layout.viewId === v.id} onClick={() => onPatch(layoutFromView(v))}>
            {v.name}
          </ViewTab>
          {layout.viewId === v.id && (
            <Menu>
              <MenuTrigger asChild>
                <button
                  type="button"
                  className="-ml-1 rounded-xs p-0.5 text-fg-3 opacity-0 hover:bg-surface-2 hover:text-fg group-hover/tab:opacity-100"
                  aria-label={`${v.name} view options`}
                >
                  <MoreHorizontal className="size-3" />
                </button>
              </MenuTrigger>
              <MenuContent>
                <MenuItem icon={<Save />} onSelect={() => void update(v)}>
                  Update with current layout
                </MenuItem>
                <MenuItem icon={<Trash2 />} danger onSelect={() => void remove(v)}>
                  Delete view
                </MenuItem>
              </MenuContent>
            </Menu>
          )}
        </div>
      ))}
      {dirty ? (
        <div className="ml-1 flex items-center gap-1">
          {current && (
            <Button size="xs" variant="ghost" onClick={() => void update(current)}>
              <Save /> Save
            </Button>
          )}
          <Button
            size="xs"
            variant="ghost"
            onClick={() => {
              setName("");
              setSaveOpen(true);
            }}
          >
            <Plus /> Save as view
          </Button>
        </div>
      ) : (
        <Tip content="Save the current filters, sort and columns as a view">
          <IconButton
            size="xs"
            label="New view"
            onClick={() => {
              setName("");
              setSaveOpen(true);
            }}
          >
            <Plus className="size-3" />
          </IconButton>
        </Tip>
      )}
      <Dialog open={saveOpen} onOpenChange={setSaveOpen}>
        <DialogContent title="Save view" description="Filters, sort, column order, widths, visibility and density" width={400}>
          <form
            className="space-y-3 px-4 py-3"
            onSubmit={(e) => {
              e.preventDefault();
              if (!name.trim()) return;
              setSaveOpen(false);
              void saveNew();
            }}
          >
            <Input autoFocus placeholder="e.g. Safe emails · ICP 80+" value={name} onChange={(e) => setName(e.target.value)} aria-label="View name" />
            <div className="flex justify-end gap-2">
              <Button variant="ghost" onClick={() => setSaveOpen(false)}>
                Cancel
              </Button>
              <Button type="submit" variant="primary" disabled={!name.trim()}>
                Save view
              </Button>
            </div>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function ViewTab({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "relative h-7 shrink-0 rounded-sm px-2.5 text-meta font-medium transition-colors",
        active ? "text-fg after:absolute after:inset-x-2 after:-bottom-[5px] after:h-[2px] after:rounded-full after:bg-accent" : "text-fg-3 hover:bg-surface-2 hover:text-fg-2",
      )}
    >
      {children}
    </button>
  );
}

/* ---- list quality summary (spec: quality strip) --------------------------------------------------- */

function QualityStrip({ listId }: { listId: string }) {
  const list = useList(listId);
  const campaignId = list.data?.source_campaign_id ?? null;
  const s = list.data?.summary;
  if (!s || s.total === 0) return campaignId ? <CampaignStrip id={campaignId} /> : null;
  const safePct = s.total ? (s.safe_emails ?? 0) / s.total : 0;
  return (
    <div className="flex shrink-0 items-center gap-4 overflow-x-auto border-b border-line bg-bg/40 px-3 py-1.5 text-meta">
      <Stat label="Leads" value={n(s.total)} />
      <Stat label="Safe emails" value={`${n(s.safe_emails ?? 0)}`} hint={pct(safePct)} tone={safePct >= 0.5 ? "success" : undefined} />
      {s.with_email !== undefined && <Stat label="With email" value={n(s.with_email)} />}
      {s.decision_maker_confidence != null && <Stat label="Decision-maker confidence" value={pct(s.decision_maker_confidence)} />}
      {s.avg_icp_score != null && <Stat label="Avg ICP" value={String(Math.round(s.avg_icp_score))} />}
      {s.boolean_columns.slice(0, 4).map((b) => (
        <Stat key={b.column_id} label={b.name} value={n(b.true_count)} hint="yes" />
      ))}
      {campaignId && <CampaignStrip id={campaignId} inline />}
    </div>
  );
}

function Stat({ label, value, hint, tone }: { label: string; value: string; hint?: string; tone?: "success" }) {
  return (
    <span className="flex shrink-0 items-baseline gap-1.5 whitespace-nowrap">
      <span className="text-fg-3">{label}</span>
      <span className={cn("tabular font-medium", tone === "success" ? "text-success" : "text-fg")}>{value}</span>
      {hint && <span className="text-fg-3">{hint}</span>}
    </span>
  );
}

function CampaignStrip({ id, inline }: { id: string; inline?: boolean }) {
  const c = useCampaign(id);
  const live = useLive((s) => s.campaigns[id]);
  if (!c.data) return null;
  const status = live?.status ?? c.data.status;
  if (!["running", "planning", "paused"].includes(status)) return null;
  const qualified = live?.qualified ?? c.data.stats?.qualified ?? 0;
  return (
    <Link href={`/campaigns/${id}`} className={cn("ml-auto flex shrink-0 items-center gap-2 text-meta text-fg-2 hover:text-fg", !inline && "border-b border-line px-3 py-1.5")}>
      <Badge tone={status === "paused" ? "warning" : "info"} dot>
        {status === "paused" ? "Paused" : "Finding leads"}
      </Badge>
      <span className="tabular">
        {n(qualified)} / {n(c.data.target)}
      </span>
      <ProgressBar value={qualified} max={Math.max(1, c.data.target)} className="w-24" />
      {live?.eta_minutes ? <span className="text-fg-3">~{live.eta_minutes} min</span> : null}
    </Link>
  );
}
