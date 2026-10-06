"use client";

import { Kbd } from "@scout/design-system";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Command } from "cmdk";
import { Building2, Columns3, Download, FolderPlus, ListChecks, MailCheck, Radar, Settings, Sparkles, Upload, User } from "lucide-react";
import { Dialog as RDialog } from "radix-ui";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import { qk, useCampaigns, useLists } from "@/lib/queries";
import { useScope, useUI } from "@/lib/store";

interface SearchResult {
  companies: { id: string; name: string; domain: string | null; city: string | null }[];
  people: { id: string; name: string; title: string | null; company: string | null; email: string | null }[];
  lists: { id: string; name: string }[];
  campaigns: { id: string; name: string; status: string }[];
}

export function CommandPalette() {
  const { paletteOpen, setPaletteOpen, setChatDraft, setImportOpen, openDrawer } = useUI();
  const router = useRouter();
  const qc = useQueryClient();
  const lists = useLists();
  const campaigns = useCampaigns();
  const [query, setQuery] = useState("");
  const [page, setPage] = useState<"root" | "create-list">("root");
  const [debounced, setDebounced] = useState("");

  useEffect(() => {
    const t = setTimeout(() => setDebounced(query), 160);
    return () => clearTimeout(t);
  }, [query]);
  const [wasOpen, setWasOpen] = useState(paletteOpen);
  if (wasOpen !== paletteOpen) {
    setWasOpen(paletteOpen);
    if (!paletteOpen) {
      setQuery("");
      setPage("root");
    }
  }

  const search = useQuery({
    queryKey: ["search", debounced],
    queryFn: () => api<SearchResult>(`search?q=${encodeURIComponent(debounced)}`),
    enabled: paletteOpen && page === "root" && debounced.trim().length >= 2,
    staleTime: 10_000,
  });

  const close = () => setPaletteOpen(false);
  const run = (fn: () => void) => () => {
    close();
    fn();
  };

  async function createList(name: string) {
    try {
      const l = await api<{ id: string }>("lists", { body: { name } });
      await qc.invalidateQueries({ queryKey: qk.lists });
      toast.success(`Created list “${name}”`);
      router.push(`/lists/${l.id}`);
    } catch (e) {
      toast.error((e as Error).message);
    }
  }

  const item =
    "flex h-9 cursor-default items-center gap-2.5 rounded-sm px-2.5 text-body text-fg-2 data-[selected=true]:bg-surface-2 data-[selected=true]:text-fg [&_svg]:size-4 [&_svg]:text-fg-3";
  const group =
    "px-1 pb-1 [&_[cmdk-group-heading]]:px-2.5 [&_[cmdk-group-heading]]:pb-1 [&_[cmdk-group-heading]]:pt-2 [&_[cmdk-group-heading]]:text-micro [&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-wide [&_[cmdk-group-heading]]:text-fg-3";
  const scope = useScope.getState();

  return (
    <RDialog.Root open={paletteOpen} onOpenChange={setPaletteOpen}>
      <RDialog.Portal>
        <RDialog.Overlay className="fixed inset-0 z-50 bg-overlay backdrop-blur-[6px] data-[state=open]:animate-fade-in" />
        <RDialog.Content
          className="fixed left-1/2 top-[14vh] z-50 w-[min(640px,calc(100vw-32px))] -translate-x-1/2 overflow-hidden rounded-lg bg-surface-1 shadow-dialog outline-none data-[state=open]:animate-fade-in"
          aria-describedby={undefined}
        >
          <RDialog.Title className="sr-only">Command palette</RDialog.Title>
          <Command label="Command palette" shouldFilter={page === "root" ? undefined : false} loop>
            <div className="flex items-center gap-2 border-b border-line px-3.5">
              <Sparkles className="size-4 text-accent" />
              <Command.Input
                autoFocus
                value={query}
                onValueChange={setQuery}
                placeholder={page === "create-list" ? "Name of the new list…" : "Search leads, companies, lists — or run a command"}
                className="h-12 flex-1 bg-transparent text-[14px] text-fg outline-none placeholder:text-fg-3"
                onKeyDown={(e) => {
                  if (page === "create-list" && e.key === "Enter" && query.trim()) {
                    e.preventDefault();
                    const name = query.trim();
                    close();
                    void createList(name);
                  }
                  if (page !== "root" && e.key === "Backspace" && !query) setPage("root");
                }}
              />
              <Kbd>esc</Kbd>
            </div>
            <Command.List className="max-h-[min(420px,60vh)] overflow-y-auto p-1 scroll-quiet">
              <Command.Empty className="px-3 py-6 text-center text-body text-fg-3">
                {page === "create-list" ? "Type a name and press Enter" : "No results. Press Enter in the chat to ask Scout instead."}
              </Command.Empty>
              {page === "root" && (
                <>
                  <Command.Group heading="Actions" className={group}>
                    <Command.Item className={item} onSelect={run(() => setChatDraft("Find "))} value="find leads discover new">
                      <Sparkles /> Find leads <span className="ml-auto text-meta text-fg-3">Ask the operator</span>
                    </Command.Item>
                    <Command.Item
                      className={item}
                      onSelect={() => {
                        setPage("create-list");
                        setQuery("");
                      }}
                      value="create list new"
                    >
                      <FolderPlus /> Create list…
                    </Command.Item>
                    <Command.Item className={item} onSelect={run(() => setChatDraft("Add a column "))} value="add enrichment create column">
                      <Columns3 /> Create column / add enrichment
                    </Command.Item>
                    <Command.Item className={item} onSelect={run(() => window.dispatchEvent(new CustomEvent("scout:export")))} value="export csv download">
                      <Download /> Export current view
                    </Command.Item>
                    <Command.Item className={item} onSelect={run(() => setImportOpen(true))} value="import csv upload">
                      <Upload /> Import CSV
                    </Command.Item>
                    <Command.Item className={item} onSelect={run(() => setChatDraft("Verify all risky emails again"))} value="run verification verify emails">
                      <MailCheck /> Run email verification
                    </Command.Item>
                    <Command.Item className={item} onSelect={run(() => router.push("/settings"))} value="open settings preferences budget">
                      <Settings /> Open settings
                    </Command.Item>
                  </Command.Group>
                  {(lists.data?.length ?? 0) > 0 && (
                    <Command.Group heading="Lists" className={group}>
                      {lists.data!.slice(0, 8).map((l) => (
                        <Command.Item key={l.id} className={item} value={`list ${l.name}`} onSelect={run(() => router.push(`/lists/${l.id}`))}>
                          <ListChecks /> {l.name}
                          <span className="ml-auto tabular text-meta text-fg-3">{l.count?.toLocaleString()}</span>
                        </Command.Item>
                      ))}
                    </Command.Group>
                  )}
                  {(campaigns.data?.length ?? 0) > 0 && (
                    <Command.Group heading="Campaigns" className={group}>
                      {campaigns.data!.slice(0, 5).map((c) => (
                        <Command.Item key={c.id} className={item} value={`campaign ${c.name}`} onSelect={run(() => router.push(`/campaigns/${c.id}`))}>
                          <Radar /> {c.name}
                          <span className="ml-auto text-meta text-fg-3">
                            {c.qualified.toLocaleString()}/{c.target.toLocaleString()} · {c.status}
                          </span>
                        </Command.Item>
                      ))}
                    </Command.Group>
                  )}
                  {search.data && (
                    <>
                      {search.data.people.length > 0 && (
                        <Command.Group heading="People" className={group}>
                          {search.data.people.map((p) => (
                            <Command.Item
                              key={p.id}
                              className={item}
                              value={`person ${p.name} ${p.email ?? ""} ${p.company ?? ""}`}
                              onSelect={run(() => openDrawer("person", p.id))}
                            >
                              <User /> <span className="truncate">{p.name}</span>
                              <span className="ml-auto truncate text-meta text-fg-3">{[p.title, p.company].filter(Boolean).join(" · ")}</span>
                            </Command.Item>
                          ))}
                        </Command.Group>
                      )}
                      {search.data.companies.length > 0 && (
                        <Command.Group heading="Companies" className={group}>
                          {search.data.companies.map((c) => (
                            <Command.Item key={c.id} className={item} value={`company ${c.name} ${c.domain ?? ""}`} onSelect={run(() => openDrawer("company", c.id))}>
                              <Building2 /> <span className="truncate">{c.name}</span>
                              <span className="ml-auto text-meta text-fg-3">{c.domain ?? c.city}</span>
                            </Command.Item>
                          ))}
                        </Command.Group>
                      )}
                    </>
                  )}
                </>
              )}
            </Command.List>
            <div className="flex h-9 items-center gap-3 border-t border-line px-3.5 text-meta text-fg-3">
              <span className="flex items-center gap-1">
                <Kbd>↑</Kbd>
                <Kbd>↓</Kbd> navigate
              </span>
              <span className="flex items-center gap-1">
                <Kbd>↵</Kbd> run
              </span>
              <span className="ml-auto flex items-center gap-1">
                Ask Scout <Kbd>⌘J</Kbd>
              </span>
              {scope.listName && <span className="truncate">in {scope.listName}</span>}
            </div>
          </Command>
        </RDialog.Content>
      </RDialog.Portal>
    </RDialog.Root>
  );
}
