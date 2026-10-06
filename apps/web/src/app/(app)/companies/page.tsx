import type { Metadata } from "next";

import { EntityTable } from "@/components/table/entity-table";

export const metadata: Metadata = { title: "Companies" };

export default function CompaniesPage() {
  return <EntityTable kind="companies" />;
}
