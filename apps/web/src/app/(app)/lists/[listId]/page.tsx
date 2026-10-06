import { ListTable } from "@/components/table/entity-table";

export default async function ListPage({ params }: { params: Promise<{ listId: string }> }) {
  const { listId } = await params;
  return <ListTable listId={listId} />;
}
