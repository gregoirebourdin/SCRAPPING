"use client";

import { cn } from "@scout/design-system";
import { Building2, ListChecks, MessageSquare, Sparkles, Users } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { useUI } from "@/lib/store";

const TABS = [
  { href: "/discover", label: "Discover", icon: Sparkles },
  { href: "/lists", label: "Lists", icon: ListChecks },
  { href: "/people", label: "People", icon: Users },
  { href: "/companies", label: "Companies", icon: Building2 },
];

/** Small screens (spec §163): list viewing, chat and lead detail — a bottom tab bar plus an assistant button.
 *  Between md and lg the sidebar is visible, so only the floating assistant button is shown. */
export function MobileNav() {
  const pathname = usePathname();
  const open = useUI((s) => s.mobileChatOpen);
  const setOpen = useUI((s) => s.setMobileChatOpen);
  return (
    <>
      <nav
        aria-label="Primary"
        className="fixed inset-x-0 bottom-0 z-30 flex h-14 items-stretch border-t border-line bg-bg/95 pb-[env(safe-area-inset-bottom)] backdrop-blur md:hidden"
      >
        {TABS.map(({ href, label, icon: Icon }) => {
          const active = pathname === href || pathname.startsWith(`${href}/`);
          return (
            <Link key={href} href={href} className={cn("flex flex-1 flex-col items-center justify-center gap-0.5 text-micro", active ? "text-fg" : "text-fg-3")}>
              <Icon className="size-[18px]" />
              {label}
            </Link>
          );
        })}
        <button
          type="button"
          onClick={() => setOpen(!open)}
          className={cn("flex flex-1 flex-col items-center justify-center gap-0.5 text-micro", open ? "text-accent-strong" : "text-fg-3")}
        >
          <MessageSquare className="size-[18px]" />
          Assistant
        </button>
      </nav>
      {!open && (
        <button
          type="button"
          onClick={() => setOpen(true)}
          aria-label="Open assistant"
          className="fixed bottom-5 right-5 z-30 hidden size-11 items-center justify-center rounded-full bg-accent text-accent-contrast shadow-dialog hover:bg-accent-strong md:flex lg:hidden"
        >
          <MessageSquare className="size-5" />
        </button>
      )}
    </>
  );
}
