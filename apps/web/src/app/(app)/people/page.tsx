import type { Metadata } from "next";

import { EntityTable } from "@/components/table/entity-table";

export const metadata: Metadata = { title: "People" };

export default function PeoplePage() {
  return <EntityTable kind="people" />;
}
