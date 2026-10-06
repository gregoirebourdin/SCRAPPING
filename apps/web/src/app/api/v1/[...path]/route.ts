/* BFF proxy: authenticates the Better Auth session, mints a 60-second service JWT and forwards the request
   (including streaming SSE responses) to the FastAPI backend. The API URL and secret never reach the browser. */
import { randomUUID } from "node:crypto";

import { SignJWT } from "jose";
import { type NextRequest, NextResponse } from "next/server";

import { auth } from "@/lib/auth";

export const dynamic = "force-dynamic";
export const maxDuration = 300;

const API_URL = (process.env.SCOUT_API_URL ?? "http://localhost:8000").replace(/\/$/, "");
const SECRET = new TextEncoder().encode(process.env.INTERNAL_API_SECRET ?? "dev-internal-secret-change-me-32-bytes-minimum!!");
const FORWARD_RESPONSE_HEADERS = ["content-type", "content-disposition", "cache-control", "x-row-count", "x-export-id"];

async function proxy(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }): Promise<Response> {
  const session = await auth.api.getSession({ headers: req.headers });
  if (!session) {
    return NextResponse.json({ error: { code: "unauthorized", message: "Sign in required" } }, { status: 401 });
  }
  const { path } = await ctx.params;
  if (path.some((p) => p === ".." || p.includes("/"))) {
    return NextResponse.json({ error: { code: "bad_request", message: "Invalid path" } }, { status: 400 });
  }
  const token = await new SignJWT({ email: session.user.email, name: session.user.name })
    .setProtectedHeader({ alg: "HS256" })
    .setSubject(session.user.id)
    .setAudience("scout-api")
    .setIssuer("scout-web")
    .setIssuedAt()
    .setExpirationTime("60s")
    .setJti(randomUUID())
    .sign(SECRET);

  const url = `${API_URL}/v1/${path.map(encodeURIComponent).join("/")}${req.nextUrl.search}`;
  const headers = new Headers({ authorization: `Bearer ${token}` });
  for (const h of ["content-type", "accept", "last-event-id"]) {
    const v = req.headers.get(h);
    if (v) headers.set(h, v);
  }
  const ws = req.cookies.get("scout_ws")?.value;
  if (ws && /^[0-9a-f-]{36}$/i.test(ws)) headers.set("x-workspace-id", ws);

  const hasBody = !["GET", "HEAD"].includes(req.method);
  let upstream: Response;
  try {
    upstream = await fetch(url, {
      method: req.method,
      headers,
      body: hasBody ? req.body : undefined,
      // @ts-expect-error -- Node fetch streaming request bodies require duplex
      duplex: hasBody ? "half" : undefined,
      cache: "no-store",
      signal: req.signal,
    });
  } catch {
    return NextResponse.json({ error: { code: "api_unreachable", message: "The Scout API is unreachable. Is it running?" } }, { status: 502 });
  }
  const out = new Headers();
  for (const h of FORWARD_RESPONSE_HEADERS) {
    const v = upstream.headers.get(h);
    if (v) out.set(h, v);
  }
  if ((upstream.headers.get("content-type") ?? "").includes("text/event-stream")) {
    out.set("cache-control", "no-cache, no-transform");
    out.set("x-accel-buffering", "no");
  }
  return new Response(upstream.body, { status: upstream.status, headers: out });
}

export { proxy as DELETE, proxy as GET, proxy as PATCH, proxy as POST, proxy as PUT };
