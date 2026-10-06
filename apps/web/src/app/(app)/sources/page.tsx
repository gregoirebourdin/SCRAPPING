import type { Metadata } from "next";

import { SourcesPage } from "@/components/admin/sources";

export const metadata: Metadata = { title: "Sources" };

export default function Page() {
  return <SourcesPage />;
}
