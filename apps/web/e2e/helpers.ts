/* Shared helpers for the browser E2E suite: account, chat operator (send / clarify / launch), live table, filters,
 * downloads, the offline world's ground truth and request log, and read-only API checks through the BFF. */
import { readFileSync } from "node:fs";
import path from "node:path";

import { test as base, expect, type Locator, type Page } from "@playwright/test";

export { expect };

const OUTPUT = path.resolve(process.env.E2E_OUTPUT_DIR ?? "e2e/.output");
const WORLD_URL = `http://127.0.0.1:${process.env.E2E_WORLD_PORT ?? 8765}`;
const ACTIVE = ["planning", "running"];

/* ---- the offline world (apps/api/tests/e2e/world.py) ----------------------------------------------- */

export interface WorldPerson {
  full_name: string;
  title: string;
  email: string | null;
  expected_status: string | null;
}
export interface WorldAgency {
  index: number;
  name: string;
  domain: string;
  city: string;
  team: WorldPerson[];
  founder: WorldPerson | null;
  marketing: WorldPerson;
  sells_instagram: boolean;
  mentions_manychat: boolean;
  deliverable_founder: boolean;
}
export interface World {
  agencies: WorldAgency[];
  imported_founders: string[];
  acme: { domain: string; ceo: string; marketing: string };
}

export function world(): World {
  return JSON.parse(readFileSync(path.join(OUTPUT, "world", "world.json"), "utf8")) as World;
}

export const worldFile = (name: string) => path.join(OUTPUT, "world", name);

/** Agency of a delivered person (by name), from the ground truth. */
export function agencyOf(w: World, fullName: string): WorldAgency {
  const a = w.agencies.find((x) => x.team.some((p) => p.full_name === fullName));
  if (!a) throw new Error(`${fullName} is not in the world`);
  return a;
}

/* ---- API shapes (read-only checks through the same-origin BFF, with the browser's session) --------- */

export interface Campaign {
  id: string;
  name: string;
  status: string;
  stop_reason: string | null;
  target: number;
  target_list_id: string;
  stats: Record<string, number>;
}
export interface Row {
  id: string;
  full_name: string | null;
  title: string | null;
  company: string | null;
  company_id: string | null;
  website: string | null;
  email: string | null;
  email_status: string | null;
  cells: Record<string, { v: unknown; d: string | null; s: string }>;
}
interface Column {
  id: string;
  name: string;
  configuration: Record<string, unknown>;
}

/* ---- browser errors fail the test ------------------------------------------------------------------ */

export const test = base.extend<{ consoleErrors: string[] }>({
  consoleErrors: [
    async ({ page }, use) => {
      const errors: string[] = [];
      page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
      page.on("console", (m) => {
        if (m.type() === "error") errors.push(`console.error: ${m.text()}`);
      });
      await use(errors);
      expect(errors, "browser errors during the scenario").toEqual([]);
    },
    { auto: true },
  ],
});

/* ---- the app ---------------------------------------------------------------------------------------- */

export class App {
  constructor(readonly page: Page) {}

  get chat(): Locator {
    return this.page.getByRole("complementary", { name: "AI operator" });
  }
  get composer(): Locator {
    return this.page.getByRole("textbox", { name: "Message Research" });
  }
  get grid(): Locator {
    return this.page.getByRole("grid");
  }
  /** Data rows of the table (live skeleton rows of in-flight companies are `aria-busy`, without an index). */
  get rows(): Locator {
    return this.grid.locator("[role=row][aria-rowindex]");
  }

  async signUp(name = "E2E Tester"): Promise<string> {
    const email = `e2e+${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`;
    await this.page.goto("/sign-up");
    await this.page.getByPlaceholder("Ada Lovelace").fill(name);
    await this.page.getByPlaceholder("you@company.com").fill(email);
    await this.page.getByPlaceholder("At least 8 characters").fill("e2e-password-123");
    await this.page.getByRole("button", { name: "Create account" }).click();
    await this.page.waitForURL(/\/discover$/);
    await expect(this.composer).toBeVisible();
    return email;
  }

  /** Send a message and wait until the operator's turn is over; returns the assistant's reply. */
  async send(text: string): Promise<Locator> {
    await expect(this.chat).toHaveAttribute("aria-busy", "false");
    await this.composer.fill(text);
    await this.composer.press("Enter");
    await expect(this.composer).toHaveValue("");
    return this.turnDone();
  }

  async turnDone(): Promise<Locator> {
    await expect(this.chat).toHaveAttribute("aria-busy", "false", { timeout: 90_000 });
    const reply = this.chat.getByTestId("assistant-message").last();
    await expect(reply).toHaveAttribute("aria-busy", "false");
    await expect(reply.getByRole("alert")).toHaveCount(0); // no error part in the reply
    return reply;
  }

  /** Answer a clarification card: option label (chip) or `{ other }` free text per question, then submit. */
  async answerClarification(card: Locator, answers: Record<string, string | { other: string }>, submit: "Continue" | "Use defaults"): Promise<Locator> {
    for (const [question, answer] of Object.entries(answers)) {
      const group = card.getByRole("radiogroup", { name: question });
      if (typeof answer === "string") {
        await group.getByRole("radio", { name: new RegExp(`^${answer}`) }).click();
        await expect(group.getByRole("radio", { name: new RegExp(`^${answer}`) })).toHaveAttribute("aria-checked", "true");
      } else {
        await group.getByRole("radio", { name: /^(Other|Autre)…$/ }).click();
        await card.getByRole("textbox", { name: question }).fill(answer.other);
      }
    }
    const replies = this.chat.getByTestId("assistant-message");
    const before = await replies.count();
    await card.getByRole("button", { name: submit }).click();
    await expect.poll(() => replies.count()).toBeGreaterThan(before);
    return this.turnDone();
  }

  /** Launch the plan card of `reply`; the conversation follows the run to its list. */
  async launchPlan(reply: Locator): Promise<{ campaignId: string; listId: string }> {
    const launched = this.page.waitForResponse((r) => /\/api\/v1\/chat\/actions\/[^/]+\/launch$/.test(new URL(r.url()).pathname) && r.request().method() === "POST");
    await reply.getByRole("button", { name: /Launch search|Lancer la recherche/ }).click();
    const res = await launched;
    expect(res.ok()).toBeTruthy();
    const campaignId = ((await res.json()) as { campaign_id: string }).campaign_id;
    await this.page.waitForURL(/\/lists\/[0-9a-f-]{36}$/);
    await expect(reply.getByText(/Plan launched|Plan lancé/)).toBeVisible();
    return { campaignId, listId: this.page.url().split("/lists/")[1]! };
  }

  /* ---- table ------------------------------------------------------------------------------------------ */

  /** Rows matching the current filters (server total, as announced to assistive tech). */
  async rowCount(): Promise<number> {
    return Number(await this.grid.getAttribute("aria-rowcount"));
  }

  async expectRowCount(n: number, timeout = 30_000): Promise<void> {
    await expect(this.grid).toHaveAttribute("aria-rowcount", String(n), { timeout });
  }

  /** Filter a boolean custom column to TRUE from its header menu (what a user does by hand). */
  async filterColumnTrue(label: string): Promise<void> {
    await this.grid.getByRole("button", { name: `${label} column menu` }).click();
    await this.page.getByRole("menuitem", { name: "Filter…" }).click();
    const builder = this.page.getByRole("dialog");
    await expect(builder.getByRole("combobox", { name: "Field" })).toHaveValue(/^cf:/);
    await expect(builder.getByRole("combobox", { name: "Operator" })).toHaveValue("is_true");
    await this.page.keyboard.press("Escape");
    await expect(builder).toBeHidden();
  }

  /** Values rendered in a column, top to bottom (all rows must fit on screen). */
  async columnTexts(label: string): Promise<string[]> {
    const headers = await this.grid.getByRole("columnheader").allInnerTexts();
    const idx = headers.findIndex((h) => h.trim() === label);
    expect(idx, `column ${label} in ${headers.join(" | ")}`).toBeGreaterThanOrEqual(0);
    const out: string[] = [];
    for (const row of await this.rows.all()) out.push((await row.getByRole("gridcell").nth(idx).innerText()).trim());
    return out;
  }

  /* ---- API (read-only checks) -------------------------------------------------------------------------- */

  async api<T>(p: string, body?: unknown): Promise<T> {
    const res = body === undefined ? await this.page.request.get(`/api/v1/${p}`) : await this.page.request.post(`/api/v1/${p}`, { data: body });
    expect(res.ok(), `${p} → ${res.status()} ${await res.text()}`).toBeTruthy();
    return (await res.json()) as T;
  }

  campaign(id: string): Promise<Campaign> {
    return this.api<Campaign>(`campaigns/${id}`);
  }

  async campaigns(): Promise<Campaign[]> {
    return this.api<Campaign[]>("campaigns");
  }

  async waitForCampaign(id: string, timeout = 180_000): Promise<Campaign> {
    let c = await this.campaign(id);
    const deadline = Date.now() + timeout;
    while (ACTIVE.includes(c.status)) {
      if (Date.now() > deadline) throw new Error(`campaign ${c.name} still ${c.status} after ${timeout / 1000}s: ${JSON.stringify(c.stats)}`);
      await this.page.waitForTimeout(500);
      c = await this.campaign(id);
    }
    return c;
  }

  /** People delivered by a campaign (its DISCOVERED exposures). */
  async delivered(campaignId: string): Promise<Row[]> {
    return (await this.api<{ rows: Row[] }>("rows/query", { scope: "campaign", campaign_id: campaignId, limit: 1000 })).rows;
  }

  async listRows(listId: string, filters?: unknown): Promise<Row[]> {
    return (await this.api<{ rows: Row[] }>("rows/query", { scope: "list", list_id: listId, filters: filters ?? null, limit: 1000 })).rows;
  }

  async column(listId: string, name: string): Promise<Column> {
    const cols = await this.api<Column[]>(`columns?list_id=${listId}`);
    const col = cols.find((c) => c.name === name);
    expect(col, `column ${name} in ${cols.map((c) => c.name).join(", ")}`).toBeTruthy();
    return col!;
  }

  /** Wait until every row of the list has a final value for the column. */
  async waitForColumn(listId: string, columnId: string): Promise<Row[]> {
    let rows: Row[] = [];
    await expect
      .poll(
        async () => {
          rows = await this.listRows(listId);
          return rows.every((r) => ["success", "unknown", "failed"].includes(r.cells[columnId]?.s ?? "missing"));
        },
        { timeout: 90_000, intervals: [500] },
      )
      .toBe(true);
    return rows;
  }

  async rejections(campaignId: string): Promise<{ name: string; domain: string; outcome: string; reason: string | null }[]> {
    return this.api(`campaigns/${campaignId}/rejections?limit=1000`);
  }

  /* ---- the world's request log ------------------------------------------------------------------------ */

  async worldRequests(): Promise<{ total: number; by_host: Record<string, number> }> {
    const res = await this.page.request.get(`${WORLD_URL}/__requests`);
    expect(res.ok()).toBeTruthy();
    return (await res.json()) as { total: number; by_host: Record<string, number> };
  }

  /** Background work (crawls of the last run) has settled: the request log stops moving. */
  async worldQuiet(): Promise<number> {
    let last = -1;
    let stable = 0;
    for (let i = 0; i < 120 && stable < 4; i++) {
      const { total } = await this.worldRequests();
      stable = total === last ? stable + 1 : 0;
      last = total;
      await this.page.waitForTimeout(500);
    }
    return last;
  }
}

/* ---- CSV ------------------------------------------------------------------------------------------- */

/** RFC 4180 parser (quoted fields, doubled quotes, CRLF), enough for the export. */
export function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  const s = text.replace(/^﻿/, "");
  for (let i = 0; i < s.length; i++) {
    const ch = s[i]!;
    if (quoted) {
      if (ch === '"' && s[i + 1] === '"') {
        field += '"';
        i++;
      } else if (ch === '"') quoted = false;
      else field += ch;
    } else if (ch === '"') quoted = true;
    else if (ch === ",") {
      row.push(field);
      field = "";
    } else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && s[i + 1] === "\n") i++;
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
    } else field += ch;
  }
  if (field || row.length) {
    row.push(field);
    rows.push(row);
  }
  return rows;
}
