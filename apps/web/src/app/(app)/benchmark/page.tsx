import type { Metadata } from "next";

import { BenchmarkPage } from "@/components/benchmark/benchmark-page";

export const metadata: Metadata = { title: "Benchmark" };

export default function Page() {
  return <BenchmarkPage />;
}
