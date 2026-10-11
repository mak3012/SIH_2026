import { login, token } from "./auth";

export const API = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
  ) {
    super(`${status}: ${detail}`);
  }
}

/** Call the API with the user's bearer token. 401 starts a login. */
export async function api<T>(path: string, init: { method?: string; json?: unknown; body?: Blob; contentType?: string } = {}): Promise<T> {
  const headers: Record<string, string> = {};
  const t = token();
  if (t) headers.Authorization = `Bearer ${t}`;
  let body: BodyInit | undefined;
  if (init.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(init.json);
  } else if (init.body) {
    headers["Content-Type"] = init.contentType ?? init.body.type ?? "application/octet-stream";
    body = init.body;
  }
  const r = await fetch(`${API}${path}`, { method: init.method ?? (body ? "POST" : "GET"), headers, body });
  if (r.status === 401) {
    void login();
    throw new ApiError(401, "signing in");
  }
  if (!r.ok) {
    let detail = r.statusText;
    try {
      const j = await r.json();
      detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch {
      /* not JSON */
    }
    throw new ApiError(r.status, detail);
  }
  return (await r.json()) as T;
}

/** POST and read server-sent events (`event:` / `data:` JSON blocks) as they arrive. */
export async function* events(path: string, json: unknown): AsyncGenerator<{ event: string; data: any }> {
  const headers: Record<string, string> = { "Content-Type": "application/json", Accept: "text/event-stream" };
  const t = token();
  if (t) headers.Authorization = `Bearer ${t}`;
  const r = await fetch(`${API}${path}`, { method: "POST", headers, body: JSON.stringify(json) });
  if (r.status === 401) {
    void login();
    throw new ApiError(401, "signing in");
  }
  if (!r.ok || !r.body) throw new ApiError(r.status, r.statusText);
  const reader = r.body.pipeThrough(new TextDecoderStream()).getReader();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += value;
    for (let i = buf.indexOf("\n\n"); i >= 0; i = buf.indexOf("\n\n")) {
      const block = buf.slice(0, i);
      buf = buf.slice(i + 2);
      const event = /^event: (.*)$/m.exec(block)?.[1];
      const data = /^data: (.*)$/m.exec(block)?.[1];
      if (event && data) yield { event, data: JSON.parse(data) };
    }
  }
}
