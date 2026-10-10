// An in-memory stand-in for the FastAPI backend, plugged into ApiClient
// as its fetch implementation. It records every request and, like the
// real API, refuses an unsafe /api/v1 request without X-CSRF-Token.

export interface FakeRequest {
  method: string;
  path: string;
  query: URLSearchParams;
  headers: Record<string, string>;
  body: unknown;
  params: Record<string, string>;
  credentials: RequestCredentials | undefined;
}

export interface FakeResponse {
  status?: number;
  body?: unknown;
  headers?: Record<string, string>;
}

export type Handler = (req: FakeRequest) => FakeResponse | Promise<FakeResponse>;

interface Route {
  method: string;
  pattern: RegExp;
  keys: string[];
  handler: Handler;
}

export const ok = (body: unknown, status = 200): FakeResponse => ({ status, body });

export const apiError = (
  status: number,
  code: string,
  message = code,
  details: Record<string, unknown> = {},
  headers: Record<string, string> = {},
): FakeResponse => ({
  status,
  body: { error: { code, message, details, request_id: "req-test" } },
  headers,
});

const SAFE = new Set(["GET", "HEAD", "OPTIONS"]);

export class FakeApi {
  readonly calls: FakeRequest[] = [];
  #routes: Route[] = [];
  csrfToken = "csrf-test-token";

  /** Register a handler. A later registration wins over an earlier one. */
  on(method: string, path: string, handler: Handler | FakeResponse): this {
    const keys: string[] = [];
    const pattern = new RegExp(
      `^${path.replace(/:([a-zA-Z]+)/g, (_, key: string) => {
        keys.push(key);

        return "([^/]+)";
      })}$`,
    );

    this.#routes.unshift({
      method,
      pattern,
      keys,
      handler: typeof handler === "function" ? handler : () => handler,
    });

    return this;
  }

  callsTo(method: string, path: string | RegExp): FakeRequest[] {
    return this.calls.filter(
      (c) =>
        c.method === method && (typeof path === "string" ? c.path === path : path.test(c.path)),
    );
  }

  fetch = async (input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> => {
    const url = new URL(String(input), "http://control-center.test");
    const method = (init.method ?? "GET").toUpperCase();
    const headers = Object.fromEntries(
      Object.entries((init.headers as Record<string, string>) ?? {}).map(([k, v]) => [
        k.toLowerCase(),
        v,
      ]),
    );
    const body = typeof init.body === "string" ? JSON.parse(init.body) : undefined;

    const request: FakeRequest = {
      method,
      path: url.pathname,
      query: url.searchParams,
      headers,
      body,
      params: {},
      credentials: init.credentials,
    };

    this.calls.push(request);

    if (
      !SAFE.has(method) &&
      url.pathname.startsWith("/api/v1/") &&
      url.pathname !== "/api/v1/auth/login"
    ) {
      if (!headers["x-csrf-token"]) {
        return toResponse(apiError(403, "CSRF_REQUIRED", "X-CSRF-Token is required."));
      }
      if (headers["x-csrf-token"] !== this.csrfToken) {
        return toResponse(apiError(403, "CSRF_INVALID", "Invalid CSRF token."));
      }
    }

    for (const route of this.#routes) {
      if (route.method !== method) continue;

      const match = route.pattern.exec(url.pathname);

      if (!match) continue;

      route.keys.forEach((key, index) => {
        request.params[key] = decodeURIComponent(match[index + 1] ?? "");
      });

      return toResponse(await route.handler(request));
    }

    return toResponse(apiError(404, "NOT_FOUND", `No fake route for ${method} ${url.pathname}`));
  };
}

function toResponse(response: FakeResponse): Response {
  const status = response.status ?? 200;

  if (status === 204 || response.body === undefined) {
    return new Response(null, { status, headers: response.headers });
  }

  return new Response(JSON.stringify(response.body), {
    status,
    headers: { "Content-Type": "application/json", ...response.headers },
  });
}
