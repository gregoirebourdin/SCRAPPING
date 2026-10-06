"use client";

import { cn, IconButton, Kbd, Menu, MenuContent, MenuItem, MenuLabel, MenuSeparator, MenuTrigger, Tip } from "@scout/design-system";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowUp, ChevronDown, History, PanelRightClose, Plus, Square } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { api, download, readSSE } from "@/lib/api";
import { qk } from "@/lib/queries";
import { useScope, useUI } from "@/lib/store";

import { Markdown } from "./markdown";
import { ToolCard } from "./tool-cards";

export interface ChatPart {
  type: "text" | "card" | "ui_effect" | "confirm" | "error" | "tool_call";
  text?: string;
  card?: Record<string, unknown> & { kind: string };
  status?: string;
  effect?: Record<string, unknown>;
  action_id?: string;
  title?: string;
  summary?: string;
  message?: string;
  tool?: string;
  call_id?: string;
}

interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  parts: ChatPart[];
  pending?: boolean;
}

const EXAMPLES = [
  "Find 300 marketing agencies in France with 2–30 employees, founders only, no leads I've seen before",
  "Add a column ManyChat",
  "Only keep SAFE emails",
  "Create a list called Hot Leads and put everyone with score > 85 into it",
];

export function useChatContext() {
  return useCallback(() => {
    const sc = useScope.getState();
    const ui = useUI.getState();
    const key = sc.scope ?? "people";
    const layout = ui.layout(key);
    return {
      list_id: sc.listId,
      list_name: sc.listName,
      view_id: layout.viewId,
      view_name: sc.viewName,
      scope: sc.listId ? "list" : sc.scope?.startsWith("companies") ? "companies" : "people",
      entity_type: sc.entityType,
      selected_ids: ui.selection[key] ?? [],
      filters: layout.filters?.conditions?.length ? layout.filters : null,
      sort: layout.sort,
      visible_columns: sc.visibleColumns,
      row_count: sc.rowCount,
      campaign_id: sc.campaignId,
    };
  }, []);
}

export function ChatPanel({ open, overlay = false }: { open: boolean; overlay?: boolean }) {
  const { chatWidth, setChatWidth, setChatOpen, chatFocusTick } = useUI();
  const listId = useScope((s) => s.listId);
  const listName = useScope((s) => s.listName);
  const scopeKey = useScope((s) => s.scope) ?? "people";
  const selectedCount = useUI((s) => (s.selection[scopeKey] ?? []).length);
  const [threadId, setThreadId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState(() => useUI.getState().chatDraft ?? "");
  const [streaming, setStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const qc = useQueryClient();
  const router = useRouter();
  const getContext = useChatContext();

  const threads = useQuery({
    queryKey: qk.threads(listId),
    queryFn: () => api<{ id: string; title: string; updated_at: string }[]>(`chat/threads${listId ? `?list_id=${listId}` : ""}`),
    enabled: open,
    staleTime: 30_000,
  });

  // Resume the latest thread for this list (spec §112: reopen a list and continue).
  const [threadListId, setThreadListId] = useState(listId);
  if (threadListId !== listId) {
    setThreadListId(listId);
    setThreadId(null);
    setMessages([]);
  }
  useEffect(() => {
    if (threadId || !threads.data?.length || messages.length) return;
    const latest = threads.data[0];
    if (!latest) return;
    void loadThread(latest.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threads.data]);

  async function loadThread(id: string) {
    const rows = await api<{ id: string; role: "user" | "assistant"; content: string; parts: ChatPart[] }[]>(`chat/threads/${id}/messages`);
    setThreadId(id);
    setMessages(rows.map((r) => ({ id: r.id, role: r.role, content: r.content, parts: r.parts ?? [] })));
  }

  useEffect(() => {
    if (chatFocusTick > 0) {
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [chatFocusTick]);
  // Drafts pushed from elsewhere (bulk bar "Ask AI", discover "Refine in chat") land in the composer.
  useEffect(() => {
    const focusEnd = () =>
      requestAnimationFrame(() => {
        const el = inputRef.current;
        if (el) {
          el.focus();
          el.setSelectionRange(el.value.length, el.value.length);
        }
      });
    if (useUI.getState().chatDraft !== null) {
      useUI.getState().setChatDraft(null);
      focusEnd();
    }
    return useUI.subscribe((st, prev) => {
      if (st.chatDraft !== null && st.chatDraft !== prev.chatDraft) {
        setInput(st.chatDraft);
        useUI.getState().setChatDraft(null);
        focusEnd();
      }
    });
  }, []);
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  const applyEffect = useCallback(
    async (eff: Record<string, unknown>) => {
      const sc = useScope.getState();
      const ui = useUI.getState();
      const key = sc.scope ?? "people";
      switch (eff.type) {
        case "set_filters":
          ui.patchLayout(key, { filters: eff.filters as never });
          break;
        case "set_sort":
          ui.patchLayout(key, { sort: eff.sort as never });
          break;
        case "select_rows":
          ui.setSelection(key, (eff.ids as string[]) ?? []);
          break;
        case "column_visibility": {
          const layout = ui.layout(key);
          ui.patchLayout(key, { columnVisibility: { ...layout.columnVisibility, [eff.column as string]: Boolean(eff.visible) } });
          break;
        }
        case "open_list":
          if (eff.list_id && eff.list_id !== sc.listId) router.push(`/lists/${eff.list_id}`);
          qc.invalidateQueries({ queryKey: qk.lists });
          break;
        case "open_view":
          ui.patchLayout(key, { viewId: eff.view_id as string });
          qc.invalidateQueries({ queryKey: ["views"] });
          break;
        case "refresh":
          qc.invalidateQueries({ queryKey: ["rows"] });
          qc.invalidateQueries({ queryKey: qk.lists });
          break;
        case "refresh_columns":
          qc.invalidateQueries({ queryKey: ["rows"] });
          qc.invalidateQueries({ queryKey: ["columns"] });
          qc.invalidateQueries({ queryKey: ["fields"] });
          break;
        case "download_export":
          try {
            const r = await download("exports", eff.request);
            toast.success(`Exported ${r.rows.toLocaleString()} rows`, { description: r.filename });
          } catch (e) {
            toast.error((e as Error).message);
          }
          break;
        case "open_import":
          ui.setImportOpen(true);
          break;
        case "list_deleted":
          qc.invalidateQueries({ queryKey: qk.lists });
          if (sc.listId === eff.list_id) router.push("/lists");
          break;
      }
    },
    [qc, router],
  );

  async function send(text: string) {
    const content = text.trim();
    if (!content || streaming) return;
    setInput("");
    const userMsg: ChatMessage = { id: `u-${Date.now()}`, role: "user", content, parts: [] };
    const asst: ChatMessage = { id: `a-${Date.now()}`, role: "assistant", content: "", parts: [], pending: true };
    setMessages((m) => [...m, userMsg, asst]);
    setStreaming(true);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    const update = (fn: (m: ChatMessage) => ChatMessage) => setMessages((all) => all.map((m) => (m.id === asst.id ? fn(m) : m)));
    try {
      const res = await fetch("/api/v1/chat/messages", {
        method: "POST",
        headers: { "content-type": "application/json", accept: "text/event-stream" },
        body: JSON.stringify({ content, thread_id: threadId, context: getContext() }),
        signal: ctrl.signal,
      });
      if (!res.ok) {
        let msg = `The assistant is unavailable (${res.status})`;
        try {
          msg = (await res.json()).error?.message ?? msg;
        } catch {
          /* ignore */
        }
        update((m) => ({ ...m, pending: false, parts: [...m.parts, { type: "error", message: msg }] }));
        return;
      }
      for await (const ev of readSSE(res, ctrl.signal)) {
        const data = ev.data ? JSON.parse(ev.data) : {};
        if (ev.event === "start") {
          if (data.thread_id && data.thread_id !== threadId) {
            setThreadId(data.thread_id);
            qc.invalidateQueries({ queryKey: qk.threads(listId) });
          }
        } else if (ev.event === "text") {
          update((m) => {
            const parts = [...m.parts];
            const last = parts[parts.length - 1];
            if (last?.type === "text") parts[parts.length - 1] = { ...last, text: (last.text ?? "") + data.delta };
            else parts.push({ type: "text", text: data.delta });
            return { ...m, content: m.content + data.delta, parts };
          });
        } else if (ev.event === "tool_call") {
          update((m) => ({ ...m, parts: [...m.parts, { type: "tool_call", tool: data.tool, title: data.title, call_id: data.call_id, status: "running" }] }));
        } else if (ev.event === "tool_result") {
          update((m) => ({
            ...m,
            parts: m.parts.filter((p) => !(p.type === "tool_call" && p.call_id === data.call_id)).concat(data.card ? [{ type: "card", card: data.card, status: data.status }] : []),
          }));
        } else if (ev.event === "ui_effect") {
          void applyEffect(data);
        } else if (ev.event === "confirm") {
          update((m) => ({ ...m, parts: [...m.parts, { type: "confirm", action_id: data.action_id, title: data.title, summary: data.summary }] }));
        } else if (ev.event === "error") {
          update((m) => ({ ...m, parts: [...m.parts, { type: "error", message: data.message }] }));
        } else if (ev.event === "done") {
          update((m) => ({ ...m, pending: false, id: data.message_id ?? m.id }));
        }
      }
    } catch (e) {
      if ((e as Error).name !== "AbortError") {
        update((m) => ({ ...m, parts: [...m.parts, { type: "error", message: "Connection lost. Your workspace is unchanged — try again." }] }));
      }
    } finally {
      setStreaming(false);
      update((m) => ({ ...m, pending: false, parts: m.parts.filter((p) => p.type !== "tool_call") }));
      abortRef.current = null;
    }
  }

  async function confirm(actionId: string, approve: boolean, msgId: string) {
    try {
      const res = await api<{ status: string; card?: ChatPart["card"]; ui_effects?: Record<string, unknown>[] }>(`chat/actions/${actionId}/confirm`, {
        body: { approve, context: getContext() },
      });
      setMessages((all) =>
        all.map((m) =>
          m.id !== msgId
            ? m
            : {
                ...m,
                parts: m.parts
                  .map((p) => (p.type === "confirm" && p.action_id === actionId ? { ...p, status: approve ? "approved" : "rejected" } : p))
                  .concat(res.card ? [{ type: "card", card: res.card, status: res.status }] : []),
              },
        ),
      );
      for (const eff of res.ui_effects ?? []) void applyEffect(eff);
    } catch (e) {
      toast.error((e as Error).message);
    }
  }

  function startResize(e: React.PointerEvent) {
    e.preventDefault();
    const startX = e.clientX;
    const start = chatWidth;
    const move = (ev: PointerEvent) => setChatWidth(start + (startX - ev.clientX));
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      document.body.style.cursor = "";
    };
    document.body.style.cursor = "col-resize";
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }

  return (
    <AnimatePresence initial={false}>
      {open && (
        <motion.aside
          key={overlay ? "chat-sheet" : "chat"}
          aria-label="AI operator"
          initial={overlay ? { x: 24, opacity: 0 } : { width: 0, opacity: 0 }}
          animate={overlay ? { x: 0, opacity: 1 } : { width: chatWidth, opacity: 1 }}
          exit={overlay ? { x: 24, opacity: 0 } : { width: 0, opacity: 0 }}
          transition={{ duration: 0.2, ease: [0.2, 0.8, 0.2, 1] }}
          className={
            overlay
              ? "fixed inset-0 z-40 flex flex-col overflow-hidden bg-bg md:inset-y-0 md:left-auto md:w-[420px] md:border-l md:border-line md:shadow-dialog"
              : "relative flex shrink-0 flex-col overflow-hidden border-l border-line bg-bg"
          }
        >
          {!overlay && (
            <div
              role="separator"
              aria-orientation="vertical"
              aria-label="Resize chat"
              onPointerDown={startResize}
              onDoubleClick={() => setChatWidth(380)}
              className="absolute inset-y-0 left-0 z-10 w-1.5 -translate-x-0.5 cursor-col-resize transition-colors hover:bg-accent/30"
            />
          )}
          <header className="flex h-11 shrink-0 items-center gap-1 border-b border-line px-3">
            <Menu>
              <MenuTrigger className="flex min-w-0 items-center gap-1 rounded-sm px-1.5 py-1 text-heading text-fg hover:bg-surface-2">
                <span className="truncate">Research</span>
                <ChevronDown className="size-3.5 text-fg-3" />
              </MenuTrigger>
              <MenuContent className="w-72">
                <MenuLabel>Recent conversations</MenuLabel>
                {(threads.data ?? []).slice(0, 12).map((t) => (
                  <MenuItem key={t.id} onSelect={() => void loadThread(t.id)} icon={<History />}>
                    {t.title}
                  </MenuItem>
                ))}
                {!threads.data?.length && <div className="px-2 py-1.5 text-meta text-fg-3">No conversations yet</div>}
                <MenuSeparator />
                <MenuItem
                  icon={<Plus />}
                  onSelect={() => {
                    setThreadId(null);
                    setMessages([]);
                  }}
                >
                  New conversation
                </MenuItem>
              </MenuContent>
            </Menu>
            <div className="flex-1" />
            <Tip content="New conversation">
              <IconButton
                label="New conversation"
                size="xs"
                onClick={() => {
                  setThreadId(null);
                  setMessages([]);
                }}
              >
                <Plus />
              </IconButton>
            </Tip>
            <Tip content="Hide" shortcut={["⌘", "\\"]}>
              <IconButton label="Hide chat" size="xs" onClick={() => (overlay ? useUI.getState().setMobileChatOpen(false) : setChatOpen(false))}>
                <PanelRightClose />
              </IconButton>
            </Tip>
          </header>
          <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto px-3 py-3 scroll-quiet">
            {messages.length === 0 ? (
              <EmptyChat onPick={(t) => void send(t)} />
            ) : (
              <div className="space-y-4">
                {messages.map((m) => (
                  <MessageView key={m.id} m={m} onConfirm={(a, ok) => void confirm(a, ok, m.id)} />
                ))}
              </div>
            )}
          </div>
          <div className="shrink-0 border-t border-line p-2.5">
            <div className="rounded-md bg-surface-1 shadow-[inset_0_0_0_1px_var(--border-strong)] focus-within:shadow-[inset_0_0_0_1px_var(--border-focus)]">
              <textarea
                ref={inputRef}
                value={input}
                rows={1}
                onChange={(e) => {
                  setInput(e.target.value);
                  e.target.style.height = "auto";
                  e.target.style.height = `${Math.min(160, e.target.scrollHeight)}px`;
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    void send(input);
                  }
                }}
                placeholder="Ask Research to find, filter, enrich, organize…"
                aria-label="Message Research"
                className="block max-h-40 min-h-[38px] w-full resize-none bg-transparent px-3 pt-2.5 text-body text-fg outline-none placeholder:text-fg-3"
              />
              <div className="flex items-center gap-2 px-2 pb-2">
                <span className="truncate text-meta text-fg-3">
                  {listName ? `In ${listName}` : "All leads"}
                  {selectedCount > 0 ? ` · ${selectedCount.toLocaleString()} selected` : ""}
                </span>
                <span className="flex-1" />
                {streaming ? (
                  <IconButton label="Stop" size="xs" variant="secondary" onClick={() => abortRef.current?.abort()}>
                    <Square className="size-3" />
                  </IconButton>
                ) : (
                  <IconButton label="Send" size="xs" variant={input.trim() ? "primary" : "secondary"} disabled={!input.trim()} onClick={() => void send(input)}>
                    <ArrowUp />
                  </IconButton>
                )}
              </div>
            </div>
            <div className="mt-1.5 flex items-center justify-between px-1 text-micro text-fg-3">
              <span>Research acts with tools. Destructive actions ask first.</span>
              <span className="flex items-center gap-1">
                <Kbd>⌘J</Kbd>
              </span>
            </div>
          </div>
        </motion.aside>
      )}
    </AnimatePresence>
  );
}

function EmptyChat({ onPick }: { onPick: (t: string) => void }) {
  return (
    <div className="flex h-full flex-col justify-end pb-2">
      <p className="text-heading text-fg">Your lead operator</p>
      <p className="mt-1 text-body text-fg-3">Describe who you want to reach, or tell Research what to do with this table.</p>
      <div className="mt-3 space-y-1">
        {EXAMPLES.map((e) => (
          <button
            key={e}
            type="button"
            onClick={() => onPick(e)}
            className="block w-full rounded-sm px-2.5 py-1.5 text-left text-body text-fg-2 shadow-[inset_0_0_0_1px_var(--border-subtle)] transition-colors hover:bg-surface-1 hover:text-fg"
          >
            {e}
          </button>
        ))}
      </div>
    </div>
  );
}

function MessageView({ m, onConfirm }: { m: ChatMessage; onConfirm: (actionId: string, approve: boolean) => void }) {
  if (m.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[88%] whitespace-pre-wrap rounded-md bg-surface-2 px-3 py-2 text-body text-fg">{m.content}</div>
      </div>
    );
  }
  const visible = m.parts.filter((p) => p.type !== "ui_effect");
  return (
    <div className="space-y-2 text-body text-fg-2">
      {visible.map((p, i) => {
        if (p.type === "text") return <Markdown key={i} text={p.text ?? ""} />;
        if (p.type === "tool_call")
          return (
            <div key={i} className="flex items-center gap-2 rounded-md px-2.5 py-2 text-meta text-fg-3 shadow-[inset_0_0_0_1px_var(--border-subtle)]">
              <span className="size-1.5 animate-pulse-soft rounded-full bg-accent" />
              {p.title ?? p.tool}…
            </div>
          );
        if (p.type === "card" && p.card) return <ToolCard key={i} card={p.card} status={p.status} />;
        if (p.type === "confirm")
          return (
            <div key={i} className="rounded-md bg-warning-soft/60 p-2.5 shadow-[inset_0_0_0_1px_color-mix(in_srgb,var(--warning)_30%,transparent)]">
              <div className="text-meta font-medium text-warning">{p.title} · confirmation required</div>
              <p className="mt-1 text-body text-fg">{p.summary}</p>
              {p.status ? (
                <p className="mt-1.5 text-meta text-fg-3">{p.status === "approved" ? "Confirmed" : "Cancelled"}</p>
              ) : (
                <div className="mt-2 flex gap-1.5">
                  <button
                    type="button"
                    onClick={() => onConfirm(p.action_id!, true)}
                    className="h-6 rounded-sm bg-danger-soft px-2 text-meta font-medium text-danger hover:bg-danger/20"
                  >
                    Confirm
                  </button>
                  <button type="button" onClick={() => onConfirm(p.action_id!, false)} className="h-6 rounded-sm px-2 text-meta text-fg-2 hover:bg-surface-2">
                    Cancel
                  </button>
                </div>
              )}
            </div>
          );
        if (p.type === "error")
          return (
            <p key={i} className="rounded-sm bg-danger-soft px-2.5 py-1.5 text-meta text-danger">
              {p.message}
            </p>
          );
        return null;
      })}
      {m.pending && visible.length === 0 && (
        <div className="flex items-center gap-1 py-1" aria-label="Thinking">
          {[0, 1, 2].map((i) => (
            <span key={i} className={cn("size-1 rounded-full bg-fg-3 animate-pulse-soft")} style={{ animationDelay: `${i * 160}ms` }} />
          ))}
        </div>
      )}
    </div>
  );
}
