"use client";

import { Button, Kbd } from "@scout/design-system";
import { Building2, CircleCheck, Inbox, Sparkles, Upload, Users } from "lucide-react";
import Link from "next/link";
import { useMemo } from "react";

import { useList } from "@/lib/queries";
import { useUI } from "@/lib/store";

import { TableView } from "./table-view";
import type { TableScope } from "./use-rows";

function EmptyHint({ icon, title, body, children }: { icon: React.ReactNode; title: string; body: string; children?: React.ReactNode }) {
  return (
    <div className="flex max-w-sm flex-col items-center text-center">
      <span className="mb-3 grid size-9 place-items-center rounded-md bg-surface-2 text-fg-3 [&_svg]:size-4">{icon}</span>
      <p className="text-body font-medium text-fg">{title}</p>
      <p className="mt-1 text-meta text-fg-3">{body}</p>
      {children && <div className="mt-4 flex items-center gap-2">{children}</div>}
    </div>
  );
}

function DiscoverButtons() {
  const setImportOpen = useUI((s) => s.setImportOpen);
  return (
    <>
      <Link href="/discover" className="inline-flex h-7 items-center gap-1.5 rounded-sm bg-accent px-2.5 text-body font-medium text-accent-contrast hover:bg-accent-strong">
        <Sparkles className="size-3.5" /> Find leads
      </Link>
      <Button onClick={() => setImportOpen(true)}>
        <Upload /> Import CSV
      </Button>
    </>
  );
}

export function EntityTable({ kind }: { kind: "people" | "companies" | "review" }) {
  const scope = useMemo<TableScope>(() => ({ key: kind, kind, listId: null, campaignId: null, entityType: kind === "companies" ? "company" : "person" }), [kind]);
  const meta = {
    people: {
      title: "People",
      icon: <Users className="size-4 text-fg-3" />,
      empty: (
        <EmptyHint icon={<Users />} title="No people yet" body="Everyone Scout discovers or you import lands here, deduplicated across all lists.">
          <DiscoverButtons />
        </EmptyHint>
      ),
    },
    companies: {
      title: "Companies",
      icon: <Building2 className="size-4 text-fg-3" />,
      empty: (
        <EmptyHint icon={<Building2 />} title="No companies yet" body="Companies are resolved once in your global registry and reused by every search.">
          <DiscoverButtons />
        </EmptyHint>
      ),
    },
    review: {
      title: "Review",
      icon: <Inbox className="size-4 text-fg-3" />,
      empty: <EmptyHint icon={<CircleCheck />} title="Nothing to review" body="Leads with conflicting sources or low identity confidence show up here for a quick human check." />,
    },
  }[kind];
  return (
    <TableView
      scope={scope}
      title={meta.title}
      icon={meta.icon}
      emptyState={meta.empty}
      subtitle={
        kind === "review" ? (
          <span className="hidden text-meta text-fg-3 md:inline">
            Select rows and approve, or open a lead to fix it · <Kbd>Space</Kbd> select
          </span>
        ) : undefined
      }
    />
  );
}

export function ListTable({ listId }: { listId: string }) {
  const list = useList(listId);
  const entityType = (list.data?.entity_type as "person" | "company" | undefined) ?? "person";
  const scope = useMemo<TableScope>(() => ({ key: `list:${listId}`, kind: "list", listId, campaignId: null, entityType }), [listId, entityType]);
  if (list.isError) {
    return (
      <div className="grid flex-1 place-items-center">
        <EmptyHint icon={<Inbox />} title="List not found" body="It may have been archived or you no longer have access.">
          <Link href="/lists" className="text-meta text-accent-strong hover:underline">
            All lists
          </Link>
        </EmptyHint>
      </div>
    );
  }
  return (
    <TableView
      key={scope.key + entityType}
      scope={scope}
      title={list.data?.name ?? "…"}
      icon={<span className="size-2 shrink-0 rounded-full" style={{ background: list.data?.color ?? "var(--text-muted)" }} />}
      emptyState={
        <EmptyHint icon={<Sparkles />} title="This list is empty" body="Ask the assistant to fill it, add leads from People, or import a CSV.">
          <DiscoverButtons />
        </EmptyHint>
      }
    />
  );
}
