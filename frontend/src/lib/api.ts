// The single place that talks to the API. Every screen goes through it, so the
// awkward parts of the contract are handled once rather than per call site.

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly body?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/**
 * FastAPI's `detail` is a string for our own raises and a ValidationError[] for
 * Pydantic's. Rendering the array straight into JSX prints "[object Object]",
 * which is how a 422 ends up looking like a frontend bug.
 */
function readDetail(body: unknown, status: number): string {
  const detail = (body as { detail?: unknown } | undefined)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((e) => {
        const loc = Array.isArray(e?.loc) ? e.loc.slice(1).join(".") : "";
        return loc ? `${loc}: ${e?.msg}` : String(e?.msg ?? e);
      })
      .join("; ");
  }
  return `Request failed (${status})`;
}

let onUnauthorized: (() => void) | null = null;

/** Set by AuthProvider so a 401 anywhere clears the token exactly once. */
export function setUnauthorizedHandler(fn: (() => void) | null) {
  onUnauthorized = fn;
}

export async function api<T>(
  method: string,
  path: string,
  opts: { body?: unknown; token?: string | null } = {},
): Promise<T> {
  const headers: Record<string, string> = {};
  if (opts.token) headers.Authorization = `Bearer ${opts.token}`;
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";

  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, {
      method,
      headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      // Next 16 does not cache fetch by default, but a stale board is a bug
      // that looks like the API ignoring writes, so say it out loud.
      cache: "no-store",
    });
  } catch (cause) {
    // A CORS rejection and a dead server are indistinguishable here: the
    // browser gives JavaScript a TypeError either way, with no detail.
    throw new ApiError(
      0,
      `Cannot reach the API at ${BASE}. Is it running, and is this origin in CORS_ORIGINS?`,
      cause,
    );
  }

  // 204 has no body at all — every DELETE returns one. Calling .json() on it
  // throws, and the caller sees a parse error instead of a success.
  if (res.status === 204) return undefined as T;

  const body = await res.json().catch(() => undefined);

  if (res.status === 401) onUnauthorized?.();
  if (!res.ok) throw new ApiError(res.status, readDetail(body, res.status), body);
  return body as T;
}

/** Build a query string, dropping undefined/null/"" so `?position=` never appears. */
export function qs(params: Record<string, string | number | null | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    search.set(key, String(value));
  }
  const out = search.toString();
  return out ? `?${out}` : "";
}
