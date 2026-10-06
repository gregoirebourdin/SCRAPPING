import { CampaignDetail } from "@/components/campaign/campaigns";

export default async function CampaignPage({ params }: { params: Promise<{ campaignId: string }> }) {
  const { campaignId } = await params;
  return <CampaignDetail id={campaignId} />;
}
