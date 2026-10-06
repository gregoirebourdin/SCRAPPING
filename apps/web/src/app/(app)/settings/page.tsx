import type { Metadata } from "next";

import { SettingsPage } from "@/components/admin/settings";

export const metadata: Metadata = { title: "Settings" };

export default function Page() {
  return <SettingsPage />;
}
