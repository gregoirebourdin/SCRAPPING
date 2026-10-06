/* Client-side API helper: every call goes through the same-origin BFF proxy (/api/v1/*). */

export class ApiError extends Error {
  code: string;
  hint?: string;
  status: number;
  details?: unknown;

  constructor(status: number, code: string, message: string, hint?: string, details?: unknown) {
    super(message);
    this.status = status;
    this.code = code;
    this.hint = hint;
    this.details = details;
  }
}

type Method = "GET" | "POST" | "PATCH" | "PUT" | "DELETE";

export async function api<T = unknown>(path: string, opts: { method?: Method; body?: unknown; signal?: AbortSignal; form?: FormData } = {}): Promise<T> {
  const method = opts.method ?? (opts.body !== undefined || opts.form ? "POST" : "GET");
  const res = await fetch(`/api/v1/${path.replace(/^\//, "")}`, {
    method,
    headers: opts.form ? undefined : opts.body !== undefined ? { "content-type": "application/json" } : undefined,
    body: opts.form ?? (opts.body !== undefined ? JSON.stringify(opts.body) : undefined),
    signal: opts.signal,
    credentials: "same-origin",
  });
  if (!res.ok) {
    let payload: { error?: { code?: string; message?: string; hint?: string; details?: unknown } } = {};
    try {
      payload = await res.json();
    } catch {
      /* non-JSON error */
    }
    const e = payload.error ?? {};
    throw new ApiError(res.status, e.code ?? "http_error", e.message ?? `Request failed (${res.status})`, e.hint, e.details);
  }
  if (res.status === 204) return undefined as T;
  const ct = res.headers.get("content-type") ?? "";
  if (ct.includes("application/json")) return (await res.json()) as T;
  return (await res.text()) as unknown as T;
}

/** Download a file response (exports). Returns the row count header when present. */
export async function download(path: string, body: unknown): Promise<{ filename: string; rows: number }> {
  const res = await fetch(`/api/v1/${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let msg = `Export failed (${res.status})`;
    try {
      msg = (await res.json()).error?.message ?? msg;
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, "export_failed", msg);
  }
  const blob = await res.blob();
  const cd = res.headers.get("content-disposition") ?? "";
  const filename = /filename="([^"]+)"/.exec(cd)?.[1] ?? "scout-export.csv";
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
  return { filename, rows: Number(res.headers.get("x-row-count") ?? 0) };
}

/** Parse a fetch() SSE body (used for POST streams such as the chat). */
export async function* readSSE(res: Response, signal?: AbortSignal): AsyncGenerator<{ event: string; data: string; id?: string }> {
  if (!res.body) return;
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  try {
    while (true) {
      if (signal?.aborted) return;
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
      let idx: number;
      while ((idx = buf.indexOf("\n\n")) !== -1) {
        const raw = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        let event = "message";
        let id: string | undefined;
        const data: string[] = [];
        for (const line of raw.split("\n")) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
          else if (line.startsWith("id:")) id = line.slice(3).trim();
        }
        if (data.length || event !== "message") yield { event, data: data.join("\n"), id };
      }
    }
  } finally {
    reader.releaseLock();
  }
}
