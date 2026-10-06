import type { Metadata } from "next";

import { CampaignsIndex } from "@/components/campaign/campaigns";

export const metadata: Metadata = { title: "Campaigns" };

export default function CampaignsPage() {
  return <CampaignsIndex />;
}
