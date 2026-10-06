import type { Metadata } from "next";

import { UsagePage } from "@/components/admin/usage";

export const metadata: Metadata = { title: "Usage" };

export default function Page() {
  return <UsagePage />;
}
