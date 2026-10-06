"use client";

import { cn, Kbd, Tip } from "@scout/design-system";
import { Activity, Building2, ChartNoAxesColumn, CircleUserRound, Database, Inbox, ListChecks, PanelLeft, Radar, Search, Settings, Sparkles, Users } from "lucide-react";
import { motion } from "motion/react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { useLists } from "@/lib/queries";
import { useUI } from "@/lib/store";

import { Logo } from "../../app/(auth)/auth-form";

const NAV = [
  { href: "/discover", label: "Discover", icon: Sparkles },
  { href: "/lists", label: "Lists", icon: ListChecks },
  { href: "/companies", label: "Companies", icon: Building2 },
  { href: "/people", label: "People", icon: Users },
  { href: "/campaigns", label: "Campaigns", icon: Radar },
  { href: "/review", label: "Review", icon: Inbox },
  { href: "/activity", label: "Activity", icon: Activity },
  { href: "/sources", label: "Sources", icon: Database },
  { href: "/usage", label: "Usage", icon: ChartNoAxesColumn },
];

export function Sidebar({ user, expanded }: { user: { name: string; email: string }; expanded: boolean }) {
  const pathname = usePathname();
  const { setSidebarExpanded, setPaletteOpen } = useUI();
  const lists = useLists();
  const width = expanded ? 220 : 56;

  const item = (href: string, label: string, Icon: React.ComponentType<{ className?: string }>, extra?: React.ReactNode) => {
    const active = pathname === href || (href !== "/" && pathname.startsWith(href + "/")) || pathname === href;
    const link = (
      <Link
        href={href}
        className={cn(
          "group relative flex h-8 items-center gap-2.5 rounded-sm px-2.5 text-body text-fg-3 transition-colors duration-[var(--dur-1)] hover:bg-surface-2 hover:text-fg",
          active && "bg-surface-2 text-fg",
        )}
        aria-current={active ? "page" : undefined}
      >
        {active && <span className="absolute left-0 top-1.5 h-5 w-[2px] rounded-full bg-accent" />}
        <Icon className={cn("size-4 shrink-0", active ? "text-fg" : "text-fg-3 group-hover:text-fg-2")} />
        {expanded && <span className="truncate">{label}</span>}
        {expanded && extra}
      </Link>
    );
    return expanded ? (
      <div key={href}>{link}</div>
    ) : (
      <Tip key={href} content={label} side="right">
        {link}
      </Tip>
    );
  };

  return (
    <motion.nav
      aria-label="Primary"
      initial={false}
      animate={{ width }}
      transition={{ duration: 0.16, ease: [0.2, 0.8, 0.2, 1] }}
      className="hidden shrink-0 flex-col bg-bg py-2 md:flex"
      style={{ width }}
    >
      <div className={cn("mb-2 flex h-8 items-center px-3", expanded ? "justify-between" : "justify-center")}>
        <Link href="/discover" className="flex items-center gap-2" aria-label="Scout home">
          <Logo size={22} />
          {expanded && <span className="text-heading text-fg">Scout</span>}
        </Link>
        {expanded && (
          <button type="button" onClick={() => setSidebarExpanded(false)} className="rounded-sm p-1 text-fg-3 hover:bg-surface-2 hover:text-fg" aria-label="Collapse sidebar">
            <PanelLeft className="size-4" />
          </button>
        )}
      </div>
      <div className="px-2">
        {expanded ? (
          <button
            type="button"
            onClick={() => setPaletteOpen(true)}
            className="mb-2 flex h-7 w-full items-center gap-2 rounded-sm bg-surface-1 px-2 text-meta text-fg-3 shadow-[inset_0_0_0_1px_var(--border-subtle)] hover:text-fg-2"
          >
            <Search className="size-3.5" />
            <span className="flex-1 text-left">Search or jump to…</span>
            <Kbd>⌘K</Kbd>
          </button>
        ) : (
          <Tip content="Search" shortcut={["⌘", "K"]} side="right">
            <button
              type="button"
              onClick={() => setPaletteOpen(true)}
              className="mb-2 flex h-8 w-full items-center justify-center rounded-sm text-fg-3 hover:bg-surface-2 hover:text-fg"
              aria-label="Search"
            >
              <Search className="size-4" />
            </button>
          </Tip>
        )}
        <div className="space-y-0.5">{NAV.map((n) => item(n.href, n.label, n.icon))}</div>
      </div>
      {expanded && (
        <div className="mt-4 min-h-0 flex-1 overflow-y-auto px-2 scroll-quiet">
          <div className="px-2.5 pb-1 text-micro uppercase tracking-wide text-fg-3">Lists</div>
          {(lists.data ?? []).slice(0, 30).map((l) => {
            const href = `/lists/${l.id}`;
            const active = pathname === href;
            return (
              <Link
                key={l.id}
                href={href}
                className={cn("flex h-7 items-center gap-2 rounded-sm px-2.5 text-body text-fg-3 hover:bg-surface-2 hover:text-fg", active && "bg-surface-2 text-fg")}
              >
                <span className="size-1.5 shrink-0 rounded-full" style={{ background: l.color ?? "var(--text-muted)" }} />
                <span className="flex-1 truncate">{l.name}</span>
                <span className="tabular text-meta text-fg-3">{l.count?.toLocaleString()}</span>
              </Link>
            );
          })}
        </div>
      )}
      {!expanded && <div className="flex-1" />}
      <div className="space-y-0.5 px-2">
        {item("/settings", "Settings", Settings)}
        {item("/account", expanded ? user.name || user.email : "Account", CircleUserRound)}
        {!expanded && (
          <Tip content="Expand sidebar" shortcut={["⌘", "B"]} side="right">
            <button
              type="button"
              onClick={() => setSidebarExpanded(true)}
              className="flex h-8 w-full items-center justify-center rounded-sm text-fg-3 hover:bg-surface-2 hover:text-fg"
              aria-label="Expand sidebar"
            >
              <PanelLeft className="size-4" />
            </button>
          </Tip>
        )}
      </div>
    </motion.nav>
  );
}
