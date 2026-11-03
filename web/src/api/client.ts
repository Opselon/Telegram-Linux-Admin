/**
 * Hand-rolled typed fetch client for /api/v1.
 *
 * - Cookie session auth (HttpOnly `tla_session` set by the backend).
 * - CSRF double-submit: reads the non-HttpOnly `tla_csrf` cookie and echoes it
 *   in `X-CSRF-Token` on every mutating request.
 * - Error envelope `{error: {code, message, details}}` → `ApiError`.
 * - 401 responses raise `ApiError` with `code === "unauthenticated"`; the auth
 *   context listens for those and redirects to /login.
 */

const API_BASE = "/api/v1";
const CSRF_COOKIE = "tla_csrf";
const CSRF_HEADER = "X-CSRF-Token";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown;

  constructor(status: number, code: string, message: string, details: unknown = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

function readCookie(name: string): string | null {
  const match = document.cookie
    .split("; ")
    .find((row) => row.startsWith(name + "="));
  return match ? decodeURIComponent(match.slice(name.length + 1)) : null;
}

type Listener = (err: ApiError) => void;

const unauthorizedListeners = new Set<Listener>();

/** Registers a callback fired whenever the API returns 401 (except on auth routes). */
export function onUnauthorized(listener: Listener): () => void {
  unauthorizedListeners.add(listener);
  return () => unauthorizedListeners.delete(listener);
}

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  /** JSON-serializable body (mutually exclusive with `bodyInit`). */
  json?: unknown;
  /** Raw body (e.g. FormData / Blob) for uploads. */
  bodyInit?: BodyInit;
  query?: Record<string, string | number | boolean | undefined>;
  signal?: AbortSignal;
  /** Skip the 401 listener broadcast (used by the auth endpoints themselves). */
  authRoute?: boolean;
}

function buildUrl(path: string, query?: RequestOptions["query"]): string {
  const url = API_BASE + path;
  if (!query) return url;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined) params.set(key, String(value));
  }
  const qs = params.toString();
  return qs ? `${url}?${qs}` : url;
}

interface ErrorEnvelope {
  error?: { code?: unknown; message?: unknown; details?: unknown };
}

async function parseError(response: Response): Promise<ApiError> {
  let code = "http_" + response.status;
  let message = response.statusText || "Request failed";
  let details: unknown = null;
  try {
    const body = (await response.json()) as ErrorEnvelope;
    if (body.error && typeof body.error === "object") {
      if (typeof body.error.code === "string") code = body.error.code;
      if (typeof body.error.message === "string") message = body.error.message;
      details = body.error.details ?? null;
    }
  } catch {
    // Non-JSON error body — keep the HTTP fallback above.
  }
  return new ApiError(response.status, code, message, details);
}

export async function api<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? "GET";
  const headers = new Headers();
  if (options.json !== undefined) headers.set("Content-Type", "application/json");
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const csrf = readCookie(CSRF_COOKIE);
    if (csrf) headers.set(CSRF_HEADER, csrf);
  }

  const response = await fetch(buildUrl(path, options.query), {
    method,
    headers,
    credentials: "same-origin",
    body: options.json !== undefined
      ? JSON.stringify(options.json)
      : options.bodyInit,
    signal: options.signal,
  });

  if (!response.ok) {
    const error = await parseError(response);
    if (response.status === 401 && !options.authRoute) {
      for (const listener of unauthorizedListeners) listener(error);
    }
    throw error;
  }

  if (response.status === 204) return undefined as T;
  const contentType = response.headers.get("Content-Type") ?? "";
  if (contentType.includes("application/json")) {
    return (await response.json()) as T;
  }
  // Non-JSON success (file download) — caller should use apiBlob instead.
  return undefined as T;
}

/** Binary download (server file content, backup archive). */
export async function apiBlob(path: string, query?: RequestOptions["query"]): Promise<Blob> {
  const response = await fetch(buildUrl(path, query), {
    method: "GET",
    credentials: "same-origin",
  });
  if (!response.ok) throw await parseError(response);
  return response.blob();
}
