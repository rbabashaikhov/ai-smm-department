// The only place in the app that calls fetch().
//
// - The session is the HttpOnly `smm_session` cookie. This module never
//   sees it: requests go out with `credentials: "include"` and the browser
//   attaches it.
// - The CSRF token lives in a private field of this object, in memory
//   only. It is never written to localStorage/sessionStorage and never put
//   into React state, and it is sent only on unsafe methods.
// - Every non-2xx response becomes an ApiError parsed from the API's
//   envelope, including Retry-After.

import { ApiError, ErrorCode, parseRetryAfter } from "./errors";

export const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS", "TRACE"]);

const DEFAULT_CSRF_HEADER = "X-CSRF-Token";

/** Unsafe endpoints that cannot present a token: no session exists yet. */
const CSRF_EXEMPT_PATHS = new Set(["/api/v1/auth/login"]);

export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

export type QueryValue = string | number | boolean | null | undefined;

export interface RequestOptions {
  query?: Record<string, QueryValue | QueryValue[]>;
  body?: unknown;
  signal?: AbortSignal;
}

export interface ApiClientOptions {
  baseUrl?: string;
  fetchImpl?: typeof fetch;
}

type UnauthorizedListener = (error: ApiError) => void;

export class ApiClient {
  readonly baseUrl: string;
  readonly #fetch: typeof fetch;
  #csrfToken: string | null = null;
  #csrfHeader = DEFAULT_CSRF_HEADER;
  #pendingCsrf: Promise<void> | null = null;
  readonly #unauthorizedListeners = new Set<UnauthorizedListener>();

  constructor(options: ApiClientOptions = {}) {
    this.baseUrl = (options.baseUrl ?? "").replace(/\/+$/, "");
    this.#fetch = options.fetchImpl ?? ((...args) => globalThis.fetch(...args));
  }

  // -- CSRF (memory only) ---------------------------------------------

  setCsrfToken(token: string, headerName: string = DEFAULT_CSRF_HEADER): void {
    this.#csrfToken = token;
    this.#csrfHeader = headerName || DEFAULT_CSRF_HEADER;
  }

  clearCsrfToken(): void {
    this.#csrfToken = null;
    this.#pendingCsrf = null;
  }

  get hasCsrfToken(): boolean {
    return this.#csrfToken !== null;
  }

  /** GET /auth/csrf and keep the token in memory. */
  async loadCsrfToken(): Promise<void> {
    const response = await this.request<{
      csrf_token: string;
      header_name: string;
    }>("GET", "/api/v1/auth/csrf");

    this.setCsrfToken(response.csrf_token, response.header_name);
  }

  // -- session expiry ---------------------------------------------------

  /** Called for any 401 except a failed login. */
  onUnauthorized(listener: UnauthorizedListener): () => void {
    this.#unauthorizedListeners.add(listener);

    return () => this.#unauthorizedListeners.delete(listener);
  }

  // -- requests ---------------------------------------------------------

  get<T>(path: string, options?: Omit<RequestOptions, "body">): Promise<T> {
    return this.request<T>("GET", path, options);
  }

  post<T>(path: string, body?: unknown, options?: RequestOptions): Promise<T> {
    return this.request<T>("POST", path, { ...options, body: body ?? {} });
  }

  async request<T>(
    method: HttpMethod,
    path: string,
    options: RequestOptions = {},
  ): Promise<T> {
    const unsafe = !SAFE_METHODS.has(method);
    const headers: Record<string, string> = { Accept: "application/json" };

    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
    }

    if (unsafe && !CSRF_EXEMPT_PATHS.has(path)) {
      if (this.#csrfToken === null) {
        // A tab that has a session but no token yet (it was opened after
        // login). One shared fetch, however many commands are waiting.
        this.#pendingCsrf ??= this.loadCsrfToken().finally(() => {
          this.#pendingCsrf = null;
        });
        await this.#pendingCsrf;
      }

      if (this.#csrfToken !== null) {
        headers[this.#csrfHeader] = this.#csrfToken;
      }
    }

    let response: Response;

    try {
      response = await this.#fetch(this.#url(path, options.query), {
        method,
        headers,
        credentials: "include",
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
        signal: options.signal,
      });
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === "AbortError") {
        throw cause;
      }

      throw new ApiError({
        status: 0,
        code: ErrorCode.NETWORK_ERROR,
        message: "The API could not be reached. Check the connection and retry.",
      });
    }

    if (response.ok) {
      if (response.status === 204) return undefined as T;

      const text = await response.text();

      return (text ? JSON.parse(text) : undefined) as T;
    }

    const error = await toApiError(response);

    if (error.status === 401 && error.code !== ErrorCode.INVALID_CREDENTIALS) {
      this.clearCsrfToken();
      this.#unauthorizedListeners.forEach((listener) => listener(error));
    }

    throw error;
  }

  #url(path: string, query?: RequestOptions["query"]): string {
    const params = new URLSearchParams();

    for (const [key, raw] of Object.entries(query ?? {})) {
      const values = Array.isArray(raw) ? raw : [raw];

      for (const value of values) {
        if (value === undefined || value === null || value === "") continue;
        params.append(key, String(value));
      }
    }

    const qs = params.toString();

    return `${this.baseUrl}${path}${qs ? `?${qs}` : ""}`;
  }
}

async function toApiError(response: Response): Promise<ApiError> {
  const retryAfter = parseRetryAfter(response.headers.get("Retry-After"));
  const headerRequestId = response.headers.get("X-Request-ID");
  let payload: unknown;

  try {
    payload = await response.json();
  } catch {
    payload = null;
  }

  const envelope =
    payload && typeof payload === "object" && "error" in payload
      ? (payload as { error: Record<string, unknown> }).error
      : null;

  if (envelope && typeof envelope.code === "string") {
    return new ApiError({
      status: response.status,
      code: envelope.code,
      message: typeof envelope.message === "string" ? envelope.message : "Request failed.",
      details:
        envelope.details && typeof envelope.details === "object"
          ? (envelope.details as Record<string, unknown>)
          : {},
      requestId:
        typeof envelope.request_id === "string" ? envelope.request_id : headerRequestId,
      retryAfter,
    });
  }

  // Not the API's envelope: a reverse proxy error page, for example.
  return new ApiError({
    status: response.status,
    code:
      response.status === 503 || response.status === 502 || response.status === 504
        ? ErrorCode.DEPENDENCY_UNAVAILABLE
        : ErrorCode.UNEXPECTED_RESPONSE,
    message: `Unexpected response from the server (HTTP ${response.status}).`,
    requestId: headerRequestId,
    retryAfter,
  });
}
