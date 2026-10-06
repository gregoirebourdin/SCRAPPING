"use client";

import type { FilterGroup, SortSpec } from "@scout/schemas";
import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

export type Density = "compact" | "comfortable";

export interface TableLayout {
  filters: FilterGroup;
  sort: SortSpec[];
  search: string;
  columnOrder: string[];
  columnSizing: Record<string, number>;
  columnVisibility: Record<string, boolean>;
  pinned: string[];
  density: Density;
  viewId: string | null;
}

export const EMPTY_FILTERS: FilterGroup = { op: "and", conditions: [] };

/** Stable empty selection — selectors must never return a fresh array (useSyncExternalStore loops). */
export const NO_IDS: string[] = Object.freeze([]) as unknown as string[]; // never mutate

export function defaultLayout(): TableLayout {
  return {
    filters: { ...EMPTY_FILTERS, conditions: [] },
    sort: [],
    search: "",
    columnOrder: [],
    columnSizing: {},
    columnVisibility: {},
    pinned: ["select", "full_name"],
    density: "compact",
    viewId: null,
  };
}

const isNarrow = () => typeof window !== "undefined" && window.innerWidth < 1024;

/* Safe storage: private windows / blocked storage must never break rendering. */
const safeStorage = createJSONStorage(() => ({
  getItem: (k: string) => {
    try {
      return localStorage.getItem(k);
    } catch {
      return null;
    }
  },
  setItem: (k: string, v: string) => {
    try {
      localStorage.setItem(k, v);
    } catch {
      /* ignore */
    }
  },
  removeItem: (k: string) => {
    try {
      localStorage.removeItem(k);
    } catch {
      /* ignore */
    }
  },
}));

interface UIState {
  sidebarExpanded: boolean;
  chatOpen: boolean;
  /** Chat as a sheet on screens narrower than the 3-column layout (not persisted). */
  mobileChatOpen: boolean;
  setMobileChatOpen: (v: boolean) => void;
  chatWidth: number;
  layouts: Record<string, TableLayout>;
  selection: Record<string, string[]>;
  /** "Select all N matching" — bulk actions target the filter, not the loaded ids. */
  allMatching: Record<string, boolean>;
  drawer: { entityType: "person" | "company"; id: string } | null;
  paletteOpen: boolean;
  importOpen: boolean;
  chatFocusTick: number;
  chatDraft: string | null;
  setChatDraft: (v: string | null) => void;
  setSidebarExpanded: (v: boolean) => void;
  setChatOpen: (v: boolean) => void;
  setChatWidth: (w: number) => void;
  layout: (scope: string) => TableLayout;
  patchLayout: (scope: string, patch: Partial<TableLayout>) => void;
  setSelection: (scope: string, ids: string[]) => void;
  setAllMatching: (scope: string, v: boolean) => void;
  openDrawer: (entityType: "person" | "company", id: string) => void;
  closeDrawer: () => void;
  setPaletteOpen: (v: boolean) => void;
  setImportOpen: (v: boolean) => void;
  focusChat: () => void;
}

export const useUI = create<UIState>()(
  persist(
    (set, get) => ({
      sidebarExpanded: false,
      chatOpen: true,
      mobileChatOpen: false,
      setMobileChatOpen: (v) => set({ mobileChatOpen: v }),
      chatWidth: 380,
      layouts: {},
      selection: {},
      allMatching: {},
      drawer: null,
      paletteOpen: false,
      importOpen: false,
      chatFocusTick: 0,
      chatDraft: null,
      setChatDraft: (v) =>
        set((st) => ({
          chatDraft: v,
          chatOpen: v ? true : st.chatOpen,
          mobileChatOpen: v && isNarrow() ? true : st.mobileChatOpen,
          chatFocusTick: v ? st.chatFocusTick + 1 : st.chatFocusTick,
        })),
      setSidebarExpanded: (v) => set({ sidebarExpanded: v }),
      setChatOpen: (v) => set({ chatOpen: v }),
      setChatWidth: (w) => set({ chatWidth: Math.max(320, Math.min(520, Math.round(w))) }),
      layout: (scope) => get().layouts[scope] ?? defaultLayout(),
      patchLayout: (scope, patch) => set((st) => ({ layouts: { ...st.layouts, [scope]: { ...(st.layouts[scope] ?? defaultLayout()), ...patch } } })),
      setSelection: (scope, ids) =>
        set((st) => ({ selection: { ...st.selection, [scope]: ids }, allMatching: ids.length ? st.allMatching : { ...st.allMatching, [scope]: false } })),
      setAllMatching: (scope, v) => set((st) => ({ allMatching: { ...st.allMatching, [scope]: v } })),
      openDrawer: (entityType, id) => set({ drawer: { entityType, id } }),
      closeDrawer: () => set({ drawer: null }),
      setPaletteOpen: (v) => set({ paletteOpen: v }),
      setImportOpen: (v) => set({ importOpen: v }),
      focusChat: () => set((st) => ({ chatOpen: true, mobileChatOpen: isNarrow() ? true : st.mobileChatOpen, chatFocusTick: st.chatFocusTick + 1 })),
    }),
    {
      name: "scout-ui",
      storage: safeStorage,
      version: 1,
      partialize: (st) => ({
        sidebarExpanded: st.sidebarExpanded,
        chatOpen: st.chatOpen,
        chatWidth: st.chatWidth,
        layouts: Object.fromEntries(Object.entries(st.layouts).map(([k, v]) => [k, { ...v, search: "" }])),
      }),
    },
  ),
);

/** The table scope currently on screen (list / people / companies / campaign), published for the chat. */
interface ScopeState {
  scope: string | null;
  listId: string | null;
  listName: string | null;
  entityType: "person" | "company";
  campaignId: string | null;
  rowCount: number | null;
  visibleColumns: string[];
  viewName: string | null;
  set: (patch: Partial<Omit<ScopeState, "set">>) => void;
}

export const useScope = create<ScopeState>()((set) => ({
  scope: null,
  listId: null,
  listName: null,
  entityType: "person",
  campaignId: null,
  rowCount: null,
  visibleColumns: [],
  viewName: null,
  set: (patch) => set(patch),
}));
