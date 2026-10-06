import type { Metadata } from "next";

import { ListsIndex } from "@/components/lists/lists-index";

export const metadata: Metadata = { title: "Lists" };

export default function ListsPage() {
  return <ListsIndex />;
}
