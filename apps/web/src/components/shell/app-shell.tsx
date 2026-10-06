"use client";

import { useEffect } from "react";

import { ChatPanel } from "@/components/chat/chat-panel";
import { ImportDialog } from "@/components/io/import-dialog";
import { LeadDrawer } from "@/components/lead/lead-drawer";
import { useUI } from "@/lib/store";
import { DESKTOP, useMediaQuery } from "@/lib/use-media";

import { CommandPalette } from "./command-palette";
import { useLiveEvents } from "./live-events";
import { MobileNav } from "./mobile-nav";
import { Sidebar } from "./sidebar";

export function AppShell({ user, children }: { user: { name: string; email: string }; children: React.ReactNode }) {
  const { chatOpen, mobileChatOpen, setChatOpen, setPaletteOpen, focusChat, setSidebarExpanded, sidebarExpanded, closeDrawer } = useUI();
  const desktop = useMediaQuery(DESKTOP);
  useLiveEvents();

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const mod = e.metaKey || e.ctrlKey;
      if (mod && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen(true);
      } else if (mod && e.key.toLowerCase() === "j") {
        e.preventDefault();
        focusChat();
      } else if (mod && e.key === "\\") {
        e.preventDefault();
        if (window.innerWidth < 1024) useUI.getState().setMobileChatOpen(!useUI.getState().mobileChatOpen);
        else setChatOpen(!useUI.getState().chatOpen);
      } else if (mod && e.key.toLowerCase() === "b") {
        e.preventDefault();
        setSidebarExpanded(!useUI.getState().sidebarExpanded);
      } else if (e.key === "Escape" && useUI.getState().drawer) {
        closeDrawer();
      } else if (e.key === "Escape" && useUI.getState().mobileChatOpen && window.innerWidth < 1024) {
        useUI.getState().setMobileChatOpen(false);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [setPaletteOpen, focusChat, setChatOpen, setSidebarExpanded, closeDrawer]);

  return (
    <div className="flex h-dvh w-full overflow-hidden bg-bg text-fg">
      <Sidebar user={user} expanded={sidebarExpanded} />
      <main className="relative flex min-w-0 flex-1 flex-col overflow-hidden bg-surface-1 pb-14 md:border-l md:border-line md:pb-0">{children}</main>
      <ChatPanel open={desktop ? chatOpen : mobileChatOpen} overlay={!desktop} />
      <MobileNav />
      <LeadDrawer />
      <CommandPalette />
      <ImportDialog />
    </div>
  );
}
