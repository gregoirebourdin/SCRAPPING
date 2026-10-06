#!/usr/bin/env node
/* Offline E2E stack for the browser suite (started by playwright.config.ts → webServer, or by hand):
 *
 *   1. fresh Postgres database (drop / create) + `alembic upgrade head`
 *   2. the offline world: agency websites + discovery manifest + email world (apps/api/tests/e2e/world.py)
 *   3. the API with in-process workers on the world (fixture discovery & verifier, local AI, crawler overrides)
 *   4. a second `next dev` on its own dist dir (NEXT_DIST_DIR) so it can run next to `pnpm dev`
 *
 * Throwaway secrets are generated per run. Everything is offline. Ctrl-C / SIGTERM stops every process.
 * Env (defaults): E2E_WEB_PORT=3020 E2E_API_PORT=8020 E2E_WORLD_PORT=8765 E2E_DB=scout_e2e
 *                 E2E_PG_URL=postgresql://postgres:postgres@localhost:5432 E2E_OUTPUT_DIR=apps/web/e2e/.output
 */
import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { createWriteStream, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import pg from "pg";

const WEB_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const API_DIR = path.resolve(WEB_DIR, "../api");
const OUT = path.resolve(process.env.E2E_OUTPUT_DIR ?? path.join(WEB_DIR, "e2e/.output"));
const WORLD_DIR = path.join(OUT, "world");
const LOG_DIR = path.join(OUT, "logs");
const WEB_PORT = Number(process.env.E2E_WEB_PORT ?? 3020);
const API_PORT = Number(process.env.E2E_API_PORT ?? 8020);
const WORLD_PORT = Number(process.env.E2E_WORLD_PORT ?? 8765);
const DB = process.env.E2E_DB ?? "scout_e2e";
const PG_URL = (process.env.E2E_PG_URL ?? "postgresql://postgres:postgres@localhost:5432").replace(/\/$/, "");
const DIST_DIR = ".next-e2e";

const children = [];
let stopping = false;
const nextEnvPath = path.join(WEB_DIR, "next-env.d.ts");
const nextEnvOriginal = existsSync(nextEnvPath) ? readFileSync(nextEnvPath, "utf8") : null;

const log = (msg) => console.log(`[e2e-stack] ${msg}`);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** `next dev` rewrites next-env.d.ts to point at its own dist dir: put the committed file back. */
function restoreNextEnv() {
  if (nextEnvOriginal !== null && readFileSync(nextEnvPath, "utf8") !== nextEnvOriginal) writeFileSync(nextEnvPath, nextEnvOriginal);
}

function start(name, cmd, args, { cwd, env }) {
  const out = createWriteStream(path.join(LOG_DIR, `${name}.log`));
  const child = spawn(cmd, args, { cwd, env: { ...process.env, ...env }, detached: true, stdio: ["ignore", "pipe", "pipe"] });
  child.stdout.pipe(out);
  child.stderr.pipe(out);
  child.output = "";
  child.stdout.on("data", (d) => (child.output = (child.output + String(d)).slice(-65536)));
  child.on("exit", (code, signal) => {
    if (!stopping) {
      console.error(`[e2e-stack] ${name} exited (${code ?? signal}) — see ${path.join(LOG_DIR, `${name}.log`)}`);
      void shutdown(1);
    }
  });
  child.label = name;
  children.push(child);
  return child;
}

function run(name, cmd, args, opts) {
  return new Promise((resolve, reject) => {
    const out = createWriteStream(path.join(LOG_DIR, `${name}.log`));
    const child = spawn(cmd, args, { cwd: opts.cwd, env: { ...process.env, ...opts.env }, stdio: ["ignore", "pipe", "pipe"] });
    child.stdout.pipe(out);
    child.stderr.pipe(out);
    child.on("exit", (code) => (code === 0 ? resolve() : reject(new Error(`${name} failed (${code}) — see ${path.join(LOG_DIR, `${name}.log`)}`))));
  });
}

async function waitFor(what, check, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (stopping) throw new Error("stopping");
    try {
      if (await check()) return;
    } catch {
      /* not up yet */
    }
    await sleep(300);
  }
  throw new Error(`${what} not ready after ${timeoutMs / 1000}s`);
}

const httpOk = (url) => async () => (await fetch(url, { redirect: "manual" })).status < 500;

async function shutdown(code = 0) {
  if (stopping) return;
  stopping = true;
  for (const c of children) {
    try {
      process.kill(-c.pid, "SIGTERM");
    } catch {
      /* already gone */
    }
  }
  const deadline = Date.now() + 8000;
  while (Date.now() < deadline && children.some((c) => c.exitCode === null && c.signalCode === null)) await sleep(100);
  for (const c of children) {
    try {
      process.kill(-c.pid, "SIGKILL");
    } catch {
      /* already gone */
    }
  }
  restoreNextEnv();
  process.exit(code);
}

process.on("SIGINT", () => void shutdown(0));
process.on("SIGTERM", () => void shutdown(0));

async function resetDatabase() {
  const admin = new pg.Client({ connectionString: `${PG_URL}/postgres` });
  await admin.connect();
  try {
    await admin.query(`DROP DATABASE IF EXISTS "${DB}" WITH (FORCE)`);
    await admin.query(`CREATE DATABASE "${DB}"`);
  } finally {
    await admin.end();
  }
}

async function main() {
  mkdirSync(LOG_DIR, { recursive: true });
  const t0 = Date.now();
  const internalSecret = randomBytes(32).toString("hex");
  const authSecret = randomBytes(32).toString("hex");
  const dbUrl = `${PG_URL}/${DB}`;
  const asyncDbUrl = dbUrl.replace(/^postgres(ql)?:\/\//, "postgresql+asyncpg://");

  log(`database ${DB}: drop / create / migrate`);
  await resetDatabase();
  await run("migrate", "uv", ["run", "alembic", "upgrade", "head"], { cwd: API_DIR, env: { DATABASE_URL: asyncDbUrl, APP_ENV: "test" } });

  log(`world on :${WORLD_PORT}`);
  const world = start("world", "uv", ["run", "python", "-m", "tests.e2e.world", WORLD_DIR, "--port", String(WORLD_PORT)], { cwd: API_DIR, env: {} });
  await waitFor("world", async () => world.output.includes("WORLD READY"), 60_000);
  const hosts = readFileSync(path.join(WORLD_DIR, "hosts.json"), "utf8");

  log(`API on :${API_PORT}`);
  start("api", "uv", ["run", "uvicorn", "scout.main:app", "--host", "127.0.0.1", "--port", String(API_PORT)], {
    cwd: API_DIR,
    env: {
      APP_ENV: "test", // no GitHub / RDAP lookups (offline), test-only backends allowed
      DATABASE_URL: asyncDbUrl,
      DATABASE_DIRECT_URL: asyncDbUrl,
      DISCOVERY_FIXTURE_MANIFEST: path.join(WORLD_DIR, "manifest.json"),
      VERIFIER_BACKEND: "fixture",
      AI_PROVIDER: "local",
      GEMINI_API_KEY: "",
      CRAWLER_HOST_OVERRIDES: hosts,
      WEB_APP_URL: `http://localhost:${WEB_PORT}`,
      CORS_ORIGINS: `http://localhost:${WEB_PORT}`,
      INTERNAL_API_SECRET: internalSecret,
      WORKER_ENABLED: "true",
      WORKER_SLOTS: "8",
      PER_DOMAIN_DELAY_MS: "150",
    },
  });
  await waitFor("API", httpOk(`http://127.0.0.1:${API_PORT}/v1/health`), 90_000);

  log(`web on :${WEB_PORT} (dist dir ${DIST_DIR})`);
  start("web", path.join(WEB_DIR, "node_modules/.bin/next"), ["dev", "--port", String(WEB_PORT)], {
    cwd: WEB_DIR,
    env: {
      NEXT_DIST_DIR: DIST_DIR,
      NEXT_TELEMETRY_DISABLED: "1",
      SCOUT_API_URL: `http://127.0.0.1:${API_PORT}`,
      BETTER_AUTH_URL: `http://localhost:${WEB_PORT}`,
      BETTER_AUTH_SECRET: authSecret,
      DATABASE_URL: dbUrl,
      INTERNAL_API_SECRET: internalSecret,
      GOOGLE_CLIENT_ID: "",
      GOOGLE_CLIENT_SECRET: "",
    },
  });
  await waitFor("web", httpOk(`http://localhost:${WEB_PORT}/sign-in`), 180_000);
  restoreNextEnv();
  log(`READY in ${((Date.now() - t0) / 1000).toFixed(1)}s — web http://localhost:${WEB_PORT} · api :${API_PORT} · world :${WORLD_PORT} · logs ${LOG_DIR}`);
}

main().catch((e) => {
  console.error(`[e2e-stack] ${e.message}`);
  void shutdown(1);
});
