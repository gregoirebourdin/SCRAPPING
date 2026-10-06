"use client";

import { Checkbox, cn, ShimmerText, Skeleton } from "@scout/design-system";
import type { FieldMeta, LeadRow, SortSpec } from "@scout/schemas";
import {
  type ColumnDef,
  columnOrderingFeature,
  columnPinningFeature,
  columnResizingFeature,
  columnSizingFeature,
  columnVisibilityFeature,
  tableFeatures,
  type Updater,
  useTable,
} from "@tanstack/react-table";
import { useVirtualizer } from "@tanstack/react-virtual";
import { ArrowDown, ArrowUp, Plus } from "lucide-react";
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useUI } from "@/lib/store";

import { copyText } from "./actions";
import type { ColumnSpec } from "./cells";
import { EditCell, type EditTarget, useCellEditor } from "./edit-cell";
import { HeaderMenu } from "./header-menu";
import { RowMenu } from "./row-menu";
import type { TableScope } from "./use-rows";

const features = tableFeatures({
  columnSizingFeature,
  columnResizingFeature,
  columnOrderingFeature,
  columnPinningFeature,
  columnVisibilityFeature,
});

type Features = typeof features;

const SELECT_W = 40;
const ROW_H = { compact: 32, comfortable: 40 } as const;

function resolve<T>(u: Updater<T>, old: T): T {
  return typeof u === "function" ? (u as (o: T) => T)(old) : u;
}

/** A candidate being worked on right now (skeleton row at the end of a list that a search is filling). */
export interface PendingRow {
  key: string;
  title: string;
  subtitle?: string | null;
  stage: string;
  paused?: boolean;
}

export interface LeadTableProps {
  scope: TableScope;
  specs: ColumnSpec[];
  rows: LeadRow[];
  total: number | null;
  loading: boolean;
  fetchingMore: boolean;
  hasMore: boolean;
  onLoadMore: () => void;
  fields: FieldMeta[];
  sort: SortSpec[];
  onSort: (sort: SortSpec[]) => void;
  onAddFilter: (field: string) => void;
  onAddColumn?: () => void;
  onConfigureColumn?: (columnId: string) => void;
  emptyState?: React.ReactNode;
  /** live run: in-flight candidates rendered as skeleton rows after the last row */
  pending?: PendingRow[];
  /** live run: more candidates queued than shown */
  pendingMore?: number;
  /** row ids that just arrived (enter animation) */
  fresh?: Record<string, number>;
  /** row id → column id → time of an in-place update (cell flash) */
  flash?: Record<string, Record<string, number>>;
}

export function LeadTable(props: LeadTableProps) {
  const { scope, specs, rows, total, loading, fetchingMore, hasMore, onLoadMore, sort, onSort } = props;
  const pending = !loading && !hasMore ? (props.pending ?? []) : [];
  const pendingMore = !loading && !hasMore ? (props.pendingMore ?? 0) : 0;
  const layout = useUI((s) => s.layouts[scope.key]);
  const patchLayout = useUI((s) => s.patchLayout);
  const selectedIds = useUI((s) => s.selection[scope.key]);
  const setSelection = useUI((s) => s.setSelection);
  const openDrawer = useUI((s) => s.openDrawer);
  const drawer = useUI((s) => s.drawer);

  const selected = useMemo(() => new Set(selectedIds ?? []), [selectedIds]);
  const density = layout?.density ?? "compact";
  const rowH = ROW_H[density];
  const primaryId = scope.entityType === "person" ? "full_name" : "company";

  const specById = useMemo(() => new Map(specs.map((s) => [s.id, s])), [specs]);

  /* ---- column state (persisted per scope) ----------------------------------------------------- */
  const columnOrder = useMemo(() => {
    const ids = specs.map((s) => s.id);
    const saved = (layout?.columnOrder ?? []).filter((id) => specById.has(id));
    return ["select", ...saved, ...ids.filter((id) => !saved.includes(id))];
  }, [layout?.columnOrder, specs, specById]);

  const pinned = useMemo(() => {
    const p = (layout?.pinned ?? ["select", primaryId]).filter((id) => id === "select" || specById.has(id));
    return ["select", ...p.filter((id) => id !== "select")];
  }, [layout?.pinned, primaryId, specById]);

  const [sizing, setSizing] = useState<Record<string, number>>(() => layout?.columnSizing ?? {});
  useEffect(() => {
    const t = setTimeout(() => patchLayout(scope.key, { columnSizing: sizing }), 400);
    return () => clearTimeout(t);
  }, [sizing, patchLayout, scope.key]);

  const visibility = useMemo(() => ({ ...(layout?.columnVisibility ?? {}), select: true }), [layout?.columnVisibility]);

  const columns = useMemo<ColumnDef<Features, LeadRow>[]>(
    () => [
      { id: "select", header: "", size: SELECT_W, minSize: SELECT_W, maxSize: SELECT_W, enableResizing: false },
      ...specs.map<ColumnDef<Features, LeadRow>>((s) => ({ id: s.id, header: s.label, size: s.width, minSize: s.minWidth ?? 64, maxSize: 640 })),
    ],
    [specs],
  );

  const table = useTable({
    features,
    columns,
    data: rows,
    getRowId: (r) => r.id,
    columnResizeMode: "onChange",
    enableColumnResizing: true,
    state: {
      columnOrder,
      columnSizing: sizing,
      columnVisibility: visibility,
      columnPinning: { start: pinned, end: [] },
    },
    onColumnSizingChange: (u) => setSizing((old) => resolve(u, old)),
    onColumnOrderChange: (u) => patchLayout(scope.key, { columnOrder: resolve(u, columnOrder).filter((id) => id !== "select") }),
    onColumnVisibilityChange: (u) => patchLayout(scope.key, { columnVisibility: resolve(u, visibility) }),
    onColumnPinningChange: (u) => patchLayout(scope.key, { pinned: resolve(u, { start: pinned, end: [] }).start }),
  });

  const leafHeaders = table.getHeaderGroups()[0]?.headers ?? [];
  const startHeaders = leafHeaders.filter((h) => h.column.getIsPinned() === "start");
  const centerHeaders = leafHeaders.filter((h) => !h.column.getIsPinned());
  const orderedHeaders = [...startHeaders, ...centerHeaders];
  const visibleColIds = orderedHeaders.map((h) => h.column.id);
  const dataColIds = visibleColIds.filter((id) => id !== "select");
  const totalWidth = table.getTotalSize();
  const stickyLeft = (id: string) => table.getColumn(id)?.getStart("start") ?? 0;
  const widthOf = (id: string) => table.getColumn(id)?.getSize() ?? 120;
  const lastPinned = startHeaders[startHeaders.length - 1]?.column.id;

  /* ---- virtualization ------------------------------------------------------------------------ */
  const scrollRef = useRef<HTMLDivElement>(null);
  const placeholderCount = loading && rows.length === 0 ? 18 : hasMore ? 6 : 0;
  const liveCount = pending.length + (pendingMore > 0 ? 1 : 0);
  const count = rows.length + liveCount + placeholderCount;
  // TanStack Virtual returns non-memoizable functions; this component opts out of compiler memoization on purpose.
  // eslint-disable-next-line react-hooks/incompatible-library
  const virtualizer = useVirtualizer({
    count,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => rowH,
    overscan: 12,
  });
  useEffect(() => virtualizer.measure(), [rowH, virtualizer]);

  const items = virtualizer.getVirtualItems();
  const lastIndex = items[items.length - 1]?.index ?? 0;
  useEffect(() => {
    if (hasMore && !fetchingMore && rows.length > 0 && lastIndex >= rows.length - 60) onLoadMore();
  }, [lastIndex, hasMore, fetchingMore, rows.length, onLoadMore]);

  /* ---- selection ----------------------------------------------------------------------------- */
  const anchor = useRef<number | null>(null);
  const toggleRow = useCallback(
    (index: number, shift: boolean) => {
      const row = rows[index];
      if (!row) return;
      const next = new Set(selected);
      if (shift && anchor.current !== null) {
        const [a, b] = [Math.min(anchor.current, index), Math.max(anchor.current, index)];
        const on = !selected.has(row.id);
        for (let i = a; i <= b; i++) {
          const r = rows[i];
          if (!r) continue;
          if (on) next.add(r.id);
          else next.delete(r.id);
        }
      } else {
        if (next.has(row.id)) next.delete(row.id);
        else next.add(row.id);
      }
      anchor.current = index;
      setSelection(scope.key, [...next]);
    },
    [rows, selected, setSelection, scope.key],
  );
  const allLoadedSelected = rows.length > 0 && rows.every((r) => selected.has(r.id));
  const someSelected = selected.size > 0 && !allLoadedSelected;

  /* ---- active cell + keyboard ---------------------------------------------------------------- */
  const [active, setActive] = useState<{ r: number; c: number } | null>(null);
  const editor = useCellEditor(scope);

  const scrollActiveIntoView = useCallback(
    (r: number, c: number) => {
      virtualizer.scrollToIndex(r, { align: "auto" });
      requestAnimationFrame(() => {
        const el = scrollRef.current?.querySelector<HTMLElement>(`[data-cell="${r}:${c}"]`);
        const sc = scrollRef.current;
        if (!el || !sc) return;
        const colId = visibleColIds[c];
        if (colId && pinned.includes(colId)) return;
        const pinnedW = pinned.reduce((acc, id) => acc + (visibleColIds.includes(id) ? widthOf(id) : 0), 0);
        const left = el.offsetLeft;
        const right = left + el.offsetWidth;
        if (left - pinnedW < sc.scrollLeft) sc.scrollLeft = left - pinnedW;
        else if (right > sc.scrollLeft + sc.clientWidth) sc.scrollLeft = right - sc.clientWidth;
      });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [virtualizer, visibleColIds.join(","), pinned],
  );

  const move = useCallback(
    (dr: number, dc: number) => {
      setActive((cur) => {
        const r = Math.max(0, Math.min(rows.length - 1, (cur?.r ?? 0) + dr));
        const c = Math.max(0, Math.min(visibleColIds.length - 1, (cur?.c ?? 1) + dc));
        scrollActiveIntoView(r, c);
        return { r, c };
      });
    },
    [rows.length, visibleColIds.length, scrollActiveIntoView],
  );

  const startEdit = useCallback(
    (r: number, c: number) => {
      const row = rows[r];
      const spec = specById.get(visibleColIds[c] ?? "");
      if (!row || !spec?.editable) return false;
      editor.begin({ row, spec });
      return true;
    },
    [rows, specById, visibleColIds, editor],
  );

  const open = useCallback(
    (row: LeadRow) => {
      if (scope.entityType === "company") openDrawer("company", row.id);
      else openDrawer("person", row.id);
    },
    [openDrawer, scope.entityType],
  );

  function onKeyDown(e: React.KeyboardEvent<HTMLDivElement>) {
    if (editor.target) return; // the editor handles its own keys
    const mod = e.metaKey || e.ctrlKey;
    const page = Math.max(1, Math.floor((scrollRef.current?.clientHeight ?? 400) / rowH) - 1);
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        move(mod ? rows.length : 1, 0);
        return;
      case "ArrowUp":
        e.preventDefault();
        move(mod ? -rows.length : -1, 0);
        return;
      case "ArrowRight":
        e.preventDefault();
        move(0, mod ? visibleColIds.length : 1);
        return;
      case "ArrowLeft":
        e.preventDefault();
        move(0, mod ? -visibleColIds.length : -1);
        return;
      case "PageDown":
        e.preventDefault();
        move(page, 0);
        return;
      case "PageUp":
        e.preventDefault();
        move(-page, 0);
        return;
      case "Home":
        e.preventDefault();
        move(0, -visibleColIds.length);
        return;
      case "End":
        e.preventDefault();
        move(0, visibleColIds.length);
        return;
      case " ":
        if (active) {
          e.preventDefault();
          toggleRow(active.r, e.shiftKey);
        }
        return;
      case "Enter":
      case "F2": {
        if (!active) return;
        e.preventDefault();
        const colId = visibleColIds[active.c];
        const row = rows[active.r];
        if (!row) return;
        if (e.key === "F2" || (colId !== primaryId && colId !== "select")) {
          if (startEdit(active.r, active.c)) return;
        }
        open(row);
        return;
      }
      case "Escape":
        if (selected.size) setSelection(scope.key, []);
        else setActive(null);
        return;
      default:
        break;
    }
    if (mod && e.key.toLowerCase() === "c" && active) {
      const row = rows[active.r];
      const spec = specById.get(visibleColIds[active.c] ?? "");
      if (row && spec) {
        e.preventDefault();
        const v = spec.copyValue?.(row) ?? "";
        if (v) copyText(String(v));
      }
    } else if (mod && e.key.toLowerCase() === "a") {
      e.preventDefault();
      setSelection(
        scope.key,
        rows.map((r) => r.id),
      );
    } else if (e.key === "x" && active) {
      toggleRow(active.r, e.shiftKey);
    }
  }

  /* ---- column drag & drop reorder -------------------------------------------------------------- */
  const [dragCol, setDragCol] = useState<string | null>(null);
  const [dropCol, setDropCol] = useState<string | null>(null);
  function onDrop(target: string) {
    if (!dragCol || dragCol === target || target === "select") return;
    const order = columnOrder.filter((id) => id !== dragCol);
    const idx = order.indexOf(target);
    order.splice(idx, 0, dragCol);
    patchLayout(scope.key, { columnOrder: order.filter((id) => id !== "select") });
    setDragCol(null);
    setDropCol(null);
  }

  const sortFor = (field?: string) => (field ? sort.find((s) => s.field === field) : undefined);

  return (
    <div
      ref={scrollRef}
      role="grid"
      aria-rowcount={total ?? rows.length}
      aria-colcount={visibleColIds.length}
      tabIndex={0}
      onKeyDown={onKeyDown}
      className="relative min-h-0 flex-1 overflow-auto overscroll-contain outline-none [scrollbar-gutter:stable]"
    >
      <div style={{ width: totalWidth + 48, minWidth: "100%" }} className="relative">
        {/* ---------- header ---------- */}
        <div role="row" className="sticky top-0 z-20 flex h-8 border-b border-line bg-surface-1" style={{ width: totalWidth + 48, minWidth: "100%" }}>
          {orderedHeaders.map((h) => {
            const id = h.column.id;
            const isPinned = h.column.getIsPinned() === "start";
            const style: React.CSSProperties = { width: h.getSize(), ...(isPinned ? { position: "sticky", left: stickyLeft(id), zIndex: 2 } : {}) };
            if (id === "select") {
              return (
                <div key={id} role="columnheader" style={style} className="flex items-center justify-center bg-surface-1">
                  <Checkbox
                    label="Select all loaded rows"
                    checked={allLoadedSelected}
                    indeterminate={someSelected}
                    onCheckedChange={(v) => setSelection(scope.key, v ? rows.map((r) => r.id) : [])}
                  />
                </div>
              );
            }
            const spec = specById.get(id);
            if (!spec) return null;
            const s = sortFor(spec.sortField);
            return (
              <div
                key={id}
                role="columnheader"
                aria-sort={s ? (s.direction === "asc" ? "ascending" : "descending") : "none"}
                draggable
                onDragStart={(e) => {
                  setDragCol(id);
                  e.dataTransfer.effectAllowed = "move";
                }}
                onDragOver={(e) => {
                  if (!dragCol) return;
                  e.preventDefault();
                  setDropCol(id);
                }}
                onDragLeave={() => setDropCol((c) => (c === id ? null : c))}
                onDrop={() => onDrop(id)}
                onDragEnd={() => {
                  setDragCol(null);
                  setDropCol(null);
                }}
                style={style}
                className={cn(
                  "group/h relative flex select-none items-center bg-surface-1 text-meta font-medium text-fg-3",
                  isPinned && id === lastPinned && "shadow-[inset_-1px_0_0_var(--border-subtle)]",
                  dropCol === id && dragCol !== id && "shadow-[inset_2px_0_0_var(--accent)]",
                  dragCol === id && "opacity-50",
                )}
              >
                <HeaderMenu
                  spec={spec}
                  sort={s}
                  isPinned={pinned.includes(id)}
                  onSort={(dir) => onSort(dir && spec.sortField ? [{ field: spec.sortField, direction: dir }] : [])}
                  onFilter={spec.filterField ? () => props.onAddFilter(spec.filterField!) : undefined}
                  onPin={(v) => patchLayout(scope.key, { pinned: v ? [...pinned, id] : pinned.filter((p) => p !== id) })}
                  onHide={() => patchLayout(scope.key, { columnVisibility: { ...visibility, [id]: false } })}
                  onConfigure={spec.custom && props.onConfigureColumn ? () => props.onConfigureColumn!(spec.custom!.id) : undefined}
                  scope={scope}
                >
                  <span className="flex min-w-0 flex-1 items-center gap-1 px-2.5 hover:text-fg-2">
                    {spec.custom && <span className="size-1.5 shrink-0 rounded-full bg-accent/70" title="Custom column" />}
                    <span className="truncate">{spec.label}</span>
                    {s && (s.direction === "asc" ? <ArrowUp className="size-3 shrink-0 text-accent" /> : <ArrowDown className="size-3 shrink-0 text-accent" />)}
                  </span>
                </HeaderMenu>
                {h.column.getCanResize() && (
                  <div
                    role="separator"
                    aria-orientation="vertical"
                    aria-label={`Resize ${spec.label}`}
                    onMouseDown={h.getResizeHandler()}
                    onTouchStart={h.getResizeHandler()}
                    onDoubleClick={() =>
                      setSizing((old) => {
                        const next = { ...old };
                        delete next[id];
                        return next;
                      })
                    }
                    className={cn(
                      "absolute -right-[3px] top-0 z-10 h-full w-[6px] cursor-col-resize after:absolute after:left-[2px] after:top-1.5 after:h-5 after:w-[2px] after:rounded-full after:bg-transparent hover:after:bg-accent/60",
                      h.column.getIsResizing() && "after:bg-accent",
                    )}
                  />
                )}
              </div>
            );
          })}
          {props.onAddColumn && (
            <button
              type="button"
              onClick={props.onAddColumn}
              className="flex h-8 w-12 shrink-0 items-center justify-center text-fg-3 hover:bg-surface-2 hover:text-fg"
              aria-label="Add column"
              title="Add an AI column"
            >
              <Plus className="size-3.5" />
            </button>
          )}
        </div>

        {/* ---------- body ---------- */}
        <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
          {items.map((vi) => {
            const row = rows[vi.index];
            const live = !row && vi.index - rows.length < liveCount ? vi.index - rows.length : -1;
            if (live >= 0) {
              const p = pending[live];
              return (
                <div
                  key={p ? `live-${p.key}` : "live-more"}
                  role="row"
                  aria-busy="true"
                  className="absolute left-0 flex items-center border-b border-line/60 text-table animate-fade-in"
                  style={{ height: rowH, transform: `translateY(${vi.start}px)`, width: totalWidth, minWidth: "100%" }}
                >
                  <div style={{ width: SELECT_W }} className="flex shrink-0 items-center justify-center">
                    <span className={cn("size-1.5 rounded-full", p?.paused ? "bg-warning/70" : "bg-accent/70 animate-pulse-soft")} />
                  </div>
                  {p ? (
                    <>
                      <div style={{ width: widthOf(dataColIds[0] ?? "") }} className="flex min-w-0 shrink-0 flex-col justify-center px-2.5 leading-tight">
                        <span className="truncate text-fg-2">{p.title}</span>
                        {rowH > 32 && p.subtitle && <span className="truncate text-micro text-fg-3">{p.subtitle}</span>}
                      </div>
                      <div style={{ width: widthOf(dataColIds[1] ?? "") }} className="min-w-0 shrink-0 truncate px-2.5 text-meta">
                        {p.paused ? <span className="text-fg-3">{p.stage}</span> : <ShimmerText>{p.stage}…</ShimmerText>}
                      </div>
                      {dataColIds.slice(2, 8).map((id, i) => (
                        <div key={id} style={{ width: widthOf(id) }} className="shrink-0 px-2.5">
                          <span className={cn("block h-2 rounded-xs skeleton-shimmer", i % 3 === 0 ? "w-3/4" : i % 3 === 1 ? "w-1/2" : "w-2/3")} />
                        </div>
                      ))}
                    </>
                  ) : (
                    <div className="px-2.5 text-meta text-fg-3">+{pendingMore.toLocaleString()} more companies queued</div>
                  )}
                </div>
              );
            }
            if (!row) {
              return (
                <div
                  key={`ph-${vi.index}`}
                  className="absolute left-0 flex items-center border-b border-line/60"
                  style={{ height: rowH, transform: `translateY(${vi.start}px)`, width: totalWidth }}
                >
                  <div style={{ width: SELECT_W }} />
                  {dataColIds.slice(0, 8).map((id, i) => (
                    <div key={id} style={{ width: widthOf(id) }} className="px-2.5">
                      <Skeleton className={cn("h-2.5", i === 0 ? "w-3/4" : i % 2 ? "w-1/2" : "w-2/3")} />
                    </div>
                  ))}
                </div>
              );
            }
            return (
              <TableRow
                key={row.id}
                row={row}
                index={vi.index}
                top={vi.start}
                height={rowH}
                width={totalWidth}
                colIds={visibleColIds}
                specById={specById}
                widthOf={widthOf}
                pinned={pinned}
                stickyLeft={stickyLeft}
                lastPinned={lastPinned}
                selected={selected.has(row.id)}
                activeCol={active?.r === vi.index ? active.c : -1}
                isOpen={drawer?.id === row.id}
                editing={editor.target?.row.id === row.id ? editor.target : null}
                fresh={Boolean(props.fresh?.[row.id])}
                flash={props.flash?.[row.id]}
                scope={scope}
                onToggle={toggleRow}
                onCellClick={(c, e) => {
                  setActive({ r: vi.index, c });
                  const t = e.target as HTMLElement;
                  if (t.closest("a,button,[role=checkbox],input")) return;
                  if (visibleColIds[c] === "select") return;
                  open(row);
                }}
                onCellDoubleClick={(c) => startEdit(vi.index, c)}
                editor={editor}
              />
            );
          })}
        </div>
      </div>
      {!loading && rows.length === 0 && liveCount === 0 && props.emptyState && (
        <div className="pointer-events-none absolute inset-x-0 top-8 bottom-0 grid place-items-center">
          <div className="pointer-events-auto">{props.emptyState}</div>
        </div>
      )}
    </div>
  );
}

interface RowProps {
  row: LeadRow;
  index: number;
  top: number;
  height: number;
  width: number;
  colIds: string[];
  specById: Map<string, ColumnSpec>;
  widthOf: (id: string) => number;
  pinned: string[];
  stickyLeft: (id: string) => number;
  lastPinned?: string;
  selected: boolean;
  activeCol: number;
  isOpen: boolean;
  editing: EditTarget | null;
  fresh?: boolean;
  flash?: Record<string, number>;
  scope: TableScope;
  onToggle: (index: number, shift: boolean) => void;
  onCellClick: (c: number, e: React.MouseEvent) => void;
  onCellDoubleClick: (c: number) => void;
  editor: ReturnType<typeof useCellEditor>;
}

const TableRow = memo(function TableRow(p: RowProps) {
  const bg = p.selected ? "bg-row-selected" : p.isOpen ? "bg-surface-2" : "bg-surface-1 group-hover/row:bg-row-hover";
  return (
    <RowMenu row={p.row} scope={p.scope}>
      <div
        role="row"
        aria-rowindex={p.index + 2}
        aria-selected={p.selected}
        className={cn("group/row absolute left-0 flex border-b border-line/60 text-table", p.fresh && "row-enter")}
        style={{ height: p.height, transform: `translateY(${p.top}px)`, width: p.width, minWidth: "100%" }}
      >
        {p.colIds.map((id, c) => {
          const isPinned = p.pinned.includes(id);
          const style: React.CSSProperties = { width: p.widthOf(id), ...(isPinned ? { position: "sticky", left: p.stickyLeft(id), zIndex: 1 } : {}) };
          const isActive = p.activeCol === c;
          if (id === "select") {
            return (
              <div
                key={id}
                role="gridcell"
                data-cell={`${p.index}:${c}`}
                style={style}
                className={cn("flex items-center justify-center", bg)}
                onClick={(e) => {
                  e.stopPropagation();
                  p.onToggle(p.index, e.shiftKey);
                }}
              >
                <span className={cn("flex items-center", !p.selected && "opacity-40 group-hover/row:opacity-100")}>
                  <Checkbox label="Select row" checked={p.selected} onCheckedChange={() => p.onToggle(p.index, false)} />
                </span>
              </div>
            );
          }
          const spec = p.specById.get(id);
          if (!spec) return null;
          const editingHere = p.editing && p.editing.spec.id === id;
          return (
            <div
              key={id}
              role="gridcell"
              data-cell={`${p.index}:${c}`}
              aria-selected={isActive}
              style={style}
              onClick={(e) => p.onCellClick(c, e)}
              onDoubleClick={() => p.onCellDoubleClick(c)}
              className={cn(
                "group/cell relative flex min-w-0 cursor-default items-center px-2.5",
                bg,
                isPinned && id === p.lastPinned && "shadow-[inset_-1px_0_0_var(--border-subtle)]",
                isActive && "z-[3] shadow-[inset_0_0_0_1.5px_var(--accent)]",
                p.flash?.[id] && "cell-flash",
              )}
            >
              {editingHere ? <EditCell target={p.editing!} editor={p.editor} /> : spec.render(p.row)}
            </div>
          );
        })}
      </div>
    </RowMenu>
  );
});

export { SELECT_W };
