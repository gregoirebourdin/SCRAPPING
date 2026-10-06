import { App, expect, test } from "./helpers";
test("probe BFF latency during a run", async ({ page }) => {
  const app = new App(page);
  await app.signUp("Probe");
  const time = async (p: string) => { const t = Date.now(); const r = await page.request.get(`/api/v1/${p}`); return `${p.slice(0, 20)}:${r.status()}:${Date.now() - t}ms`; };
  console.log("idle", await time("me"), await time("campaigns"));
  const reply = await app.send("Find 12 French marketing agencies. Founder/CEO only. Do not include leads already seen.");
  const { campaignId } = await app.launchPlan(reply);
  for (let i = 0; i < 8; i++) {
    const inPage = await page.evaluate(async (id) => { const t = performance.now(); await fetch(`/api/v1/campaigns/${id}`); return Math.round(performance.now() - t); }, campaignId);
    console.log("run", i, await time("me"), await time(`campaigns/${campaignId}`), "in-page", inPage);
    await page.waitForTimeout(1000);
  }
  await expect(page.getByText("Completed — 12 qualified leads").first()).toBeVisible({ timeout: 120_000 });
  console.log("after", await time("me"), await time(`campaigns/${campaignId}`));
});
