/* Brief §200 — the critical flow, entirely through the chat operator and the table:
 * search → leads stream in → list → AI column → filter TRUE → add to list → keyword column on the cached sites
 * (no re-crawl) → SAFE only → export → same search again excludes everyone already delivered. */
import { readFileSync } from "node:fs";

import { agencyOf, App, expect, parseCsv, test, world } from "./helpers";

const REQUEST = "Find 12 French marketing agencies. Founder/CEO only. Do not include leads already seen.";
const EXPORT_HEADER = [
  ...["Full name", "Title", "Company", "Website", "Email", "Email status", "Public profile", "City", "Country"],
  ...["Employees min", "Employees max", "Industry", "ICP score", "Confidence", "Sources", "Updated"],
  ...["Offers Instagram", "Mentions ManyChat"],
];

test("200 · find → list → AI column → filter → add → cached keyword column → SAFE → export → rerun excludes seen people", async ({ page }) => {
  const app = new App(page);
  const w = world();

  await test.step("create an account", () => app.signUp("Critical Flow"));

  const plan = await test.step("precise request → plan card, no questions", async () => {
    const reply = await app.send(REQUEST);
    await expect(reply.getByText("Search plan")).toBeVisible();
    await expect(reply.getByRole("radiogroup")).toHaveCount(0);
    for (const v of ["12 new leads", "Marketing agency", "France", "Founder / CEO"]) await expect(reply.getByText(v, { exact: true })).toBeVisible();
    await expect(reply.getByText(/^People excluded/)).toBeVisible();
    return reply;
  });

  const { campaignId, listId } = await test.step("launch → the conversation follows the run to its list", () => app.launchPlan(plan));

  const first = await test.step("leads progressively enter the table", async () => {
    // Sample the table only (the status API is slow while workers run): row count + live rows of analysed companies.
    const counts: number[] = [];
    let sawInFlight = false;
    const done = page.getByText("Completed — 12 qualified leads").first();
    const deadline = Date.now() + 180_000;
    while (!(await done.isVisible()) && Date.now() < deadline) {
      counts.push(await app.rowCount());
      sawInFlight ||= (await app.grid.locator("[role=row][aria-busy=true]").count()) > 0;
      await page.waitForTimeout(250);
    }
    await expect(done).toBeVisible();
    await app.expectRowCount(12);
    counts.push(12);
    // the count only grows, and was seen strictly between 0 and the final number (streaming, not one batch)
    expect(counts, counts.join(",")).toEqual([...counts].sort((a, b) => a - b));
    expect(counts.some((n) => n > 0 && n < 12), counts.join(",")).toBe(true);
    expect(sawInFlight, "companies being analysed are shown as live rows").toBe(true);
    const c = await app.waitForCampaign(campaignId);
    expect(c.status, c.stop_reason ?? "").toBe("completed");
    expect(c.stats.qualified).toBe(12);
    const people = await app.delivered(campaignId);
    expect(people).toHaveLength(12);
    for (const p of people) {
      const a = agencyOf(w, p.full_name!);
      expect(a.founder?.full_name, `${p.full_name} is the founder of ${a.name}`).toBe(p.full_name);
      expect(p.email_status, `${p.full_name} <${p.email}>`).toBe(a.founder?.expected_status);
    }
    return people;
  });

  await test.step("“Create a list called Instagram Agencies.”", async () => {
    const reply = await app.send("Create a list called Instagram Agencies.");
    await expect(reply.getByText("Created list Instagram Agencies")).toBeVisible();
    const lists = await app.api<{ id: string; name: string; count: number }[]>("lists");
    expect(lists.find((l) => l.name === "Instagram Agencies")?.count).toBe(0);
  });

  const instagram = await test.step("AI column “Offers Instagram” is created and enriched from the cached sites", async () => {
    const reply = await app.send("Add a column called Offers Instagram and determine whether they actually sell Instagram management.");
    await expect(reply.getByText("Created “Offers Instagram”", { exact: true })).toBeVisible();
    await expect(reply.getByText("12 / 12 already cached")).toBeVisible();
    const col = await app.column(listId, "Offers Instagram");
    const rows = await app.waitForColumn(listId, col.id);
    const truth = new Map(rows.map((r) => [r.full_name!, agencyOf(w, r.full_name!).sells_instagram]));
    for (const r of rows) expect(r.cells[col.id]?.d, `${r.company}: ${JSON.stringify(r.cells[col.id])}`).toBe(String(truth.get(r.full_name!)));
    const yes = [...truth.values()].filter(Boolean).length;
    expect(yes).toBeGreaterThan(0);
    expect(yes).toBeLessThan(12);
    await expect(app.grid.getByRole("columnheader", { name: "Offers Instagram" })).toBeVisible();
    await expect.poll(async () => (await app.columnTexts("Offers Instagram")).sort().join(",")).toBe([...truth.values()].map((v) => (v ? "Yes" : "No")).sort().join(","));
    return { col, sellers: rows.filter((r) => truth.get(r.full_name!)) };
  });

  await test.step("the user filters the column to TRUE", async () => {
    await app.filterColumnTrue("Offers Instagram");
    await app.expectRowCount(instagram.sellers.length);
    expect(await app.columnTexts("Offers Instagram")).toEqual(instagram.sellers.map(() => "Yes"));
  });

  await test.step("“Put those into Instagram Agencies.”", async () => {
    const reply = await app.send("Put those into Instagram Agencies.");
    await expect(reply.getByText("Added to Instagram Agencies", { exact: true })).toBeVisible();
    await expect(reply.getByText(`${instagram.sellers.length} added`)).toBeVisible();
    const lists = await app.api<{ id: string; name: string }[]>("lists");
    const target = lists.find((l) => l.name === "Instagram Agencies")!;
    const members = await app.listRows(target.id);
    expect(members.map((m) => m.full_name).sort()).toEqual(instagram.sellers.map((s) => s.full_name).sort());
  });

  await test.step("“Add whether they mention ManyChat.” → keyword column on the cached sites, no page fetched", async () => {
    const before = await app.worldQuiet();
    const reply = await app.send("Add whether they mention ManyChat.");
    await expect(reply.getByText("Created “Mentions ManyChat”", { exact: true })).toBeVisible();
    await expect(reply.getByText("Website keyword match", { exact: true })).toBeVisible();
    await expect(reply.getByText("12 / 12 already cached")).toBeVisible();
    const col = await app.column(listId, "Mentions ManyChat");
    const rows = await app.waitForColumn(listId, col.id);
    for (const r of rows) expect(r.cells[col.id]?.d, r.company!).toBe(String(agencyOf(w, r.full_name!).mentions_manychat));
    expect(rows.some((r) => r.cells[col.id]?.d === "true")).toBe(true);
    await page.waitForTimeout(1500);
    expect((await app.worldRequests()).total, "no website was fetched again").toBe(before);
    await expect(app.grid.getByRole("columnheader", { name: "Mentions ManyChat" })).toBeVisible();
  });

  const safe = await test.step("“Only keep SAFE emails.” → only SAFE badges remain", async () => {
    const expected = instagram.sellers.filter((s) => s.email_status === "SAFE");
    expect(expected.length, "the Instagram sellers mix SAFE and LIKELY_SAFE emails").toBeLessThan(instagram.sellers.length);
    expect(expected.length).toBeGreaterThan(0);
    const reply = await app.send("Only keep SAFE emails.");
    await expect(reply.getByText(`${expected.length} rows match.`)).toBeVisible();
    await app.expectRowCount(expected.length);
    await expect.poll(() => app.columnTexts("Email status")).toEqual(expected.map(() => "Safe"));
    return expected;
  });

  await test.step("export → CSV download of exactly the filtered rows", async () => {
    const download = page.waitForEvent("download");
    const reply = await app.send("Export");
    await expect(reply.getByText("Export ready", { exact: true })).toBeVisible();
    const file = await download;
    expect(file.suggestedFilename()).toMatch(/\.csv$/);
    const [header, ...lines] = parseCsv(readFileSync(await file.path(), "utf8")).filter((r) => r.some((c) => c !== ""));
    expect(header).toEqual(EXPORT_HEADER); // the visible table columns, the two custom columns last
    expect(lines).toHaveLength(safe.length);
    const col = (h: string) => header!.indexOf(h);
    expect(lines.map((l) => l[col("Full name")]).sort()).toEqual(safe.map((s) => s.full_name).sort());
    expect(new Set(lines.map((l) => l[col("Email status")]))).toEqual(new Set(["SAFE"]));
    expect(new Set(lines.map((l) => l[col("Offers Instagram")]))).toEqual(new Set(["true"]));
    await expect(page.getByText(`Exported ${safe.length} rows`)).toBeVisible();
  });

  await test.step("start the same campaign again → nobody already delivered comes back", async () => {
    const reply = await app.send("Start the same campaign again");
    await expect(reply.getByText("Campaign started again", { exact: true })).toBeVisible();
    const rerun = (await app.campaigns()).find((c) => c.id !== campaignId && c.target_list_id === listId);
    expect(rerun, "the rerun feeds the same list").toBeTruthy();
    const done = await app.waitForCampaign(rerun!.id);
    expect(done.status, done.stop_reason ?? "").toBe("completed");
    expect(done.stats.excluded_previous, "previously delivered founders were reached and excluded").toBeGreaterThan(0);
    const again = await app.delivered(rerun!.id);
    expect(again).toHaveLength(12);
    const seen = new Set(first.map((p) => p.id));
    expect(again.filter((p) => seen.has(p.id)).map((p) => p.full_name)).toEqual([]);
    // the table (filters cleared) now holds both runs, without duplicates
    await page.getByRole("button", { name: "Clear", exact: true }).click();
    await app.expectRowCount(24);
  });
});
