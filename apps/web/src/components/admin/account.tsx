"use client";

import { Button, Segmented } from "@scout/design-system";
import { CircleUserRound, LogOut } from "lucide-react";
import { useRouter } from "next/navigation";

import { Block, Page, Panel } from "@/components/common/page";
import { Avatar } from "@/components/table/cells";
import { signOut } from "@/lib/auth-client";
import { useMe } from "@/lib/queries";
import { type ThemePref, useTheme } from "@/lib/theme";

export function AccountPage() {
  const me = useMe();
  const router = useRouter();
  const [pref, setPref] = useTheme();
  return (
    <Page title="Account" icon={<CircleUserRound className="size-4 text-fg-3" />} width="narrow">
      <Block title="Profile">
        <Panel className="flex items-center gap-3 p-4">
          <Avatar name={me.data?.name || me.data?.email} size={36} />
          <div className="min-w-0 flex-1">
            <div className="truncate text-body font-medium text-fg">{me.data?.name || "—"}</div>
            <div className="truncate text-meta text-fg-3">{me.data?.email}</div>
          </div>
          <Button
            variant="ghost"
            onClick={async () => {
              await signOut();
              router.replace("/sign-in");
              router.refresh();
            }}
          >
            <LogOut /> Sign out
          </Button>
        </Panel>
      </Block>
      <Block title="Appearance">
        <Panel className="flex items-center justify-between p-4">
          <div>
            <div className="text-body text-fg">Theme</div>
            <div className="text-meta text-fg-3">Dark is the default; light follows the same tokens</div>
          </div>
          <Segmented<ThemePref>
            value={pref}
            onChange={setPref}
            options={[
              { value: "dark", label: "Dark" },
              { value: "light", label: "Light" },
              { value: "system", label: "System" },
            ]}
          />
        </Panel>
      </Block>
      <Block title="Workspaces">
        <Panel className="divide-y divide-line">
          {(me.data?.workspaces ?? []).map((w) => (
            <div key={w.id} className="flex items-center justify-between px-4 py-2.5 text-body">
              <span className="text-fg">{w.name}</span>
              <span className="text-meta text-fg-3">
                {w.role}
                {w.id === me.data?.current_workspace_id ? " · current" : ""}
              </span>
            </div>
          ))}
        </Panel>
      </Block>
    </Page>
  );
}
