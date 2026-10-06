import type { Metadata } from "next";

import { RunDetailView } from "@/components/benchmark/run-detail";

export const metadata: Metadata = { title: "Benchmark run" };

export default async function BenchmarkRunPage({ params }: { params: Promise<{ runId: string }> }) {
  const { runId } = await params;
  return <RunDetailView id={runId} />;
}
