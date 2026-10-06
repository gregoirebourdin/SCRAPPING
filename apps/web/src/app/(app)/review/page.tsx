import type { Metadata } from "next";

import { EntityTable } from "@/components/table/entity-table";

export const metadata: Metadata = { title: "Review" };

export default function ReviewPage() {
  return <EntityTable kind="review" />;
}
