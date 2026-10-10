import { describe, expect, it, vi } from "vitest";

import { apiError, FakeApi, ok } from "../test/fakeApi";
import { ApiClient } from "./client";
import {
  ApiError,
  isInvalidStateTransition,
  isPublicationNotEditable,
  isStaleRevision,
  isUnavailable,
  isVersionConflict,
  parseRetryAfter,
} from "./errors";

function setup() {
  const server = new FakeApi();
  server
    .on("GET", "/api/v1/auth/csrf", ok({ csrf_token: server.csrfToken, header_name: "X-CSRF-Token" }))
    .on("GET", "/api/v1/thing", ok({ ok: true }))
    .on("POST", "/api/v1/thing", ok({ ok: true }))
    .on("POST", "/api/v1/auth/login", ok({ user: {}, csrf_token: "x" }));
  const client = new ApiClient({ fetchImpl: server.fetch });

  return { server, client };
}

describe("ApiClient CSRF", () => {
  it("sends the CSRF header on unsafe methods only", async () => {
    const { server, client } = setup();
    await client.loadCsrfToken();

    await client.get("/api/v1/thing");
    await client.post("/api/v1/thing", { a: 1 });

    const [get] = server.callsTo("GET", "/api/v1/thing");
    const [post] = server.callsTo("POST", "/api/v1/thing");

    expect(get!.headers["x-csrf-token"]).toBeUndefined();
    expect(post!.headers["x-csrf-token"]).toBe(server.csrfToken);
  });

  it("does not send a token to login, which has no session yet", async () => {
    const { server, client } = setup();
    client.setCsrfToken("stale-token");

    await client.post("/api/v1/auth/login", { email: "a@b.c", password: "pw" });

    expect(server.callsTo("POST", "/api/v1/auth/login")[0]!.headers["x-csrf-token"]).toBeUndefined();
  });

  it("fetches the token lazily once before the first unsafe request", async () => {
    const { server, client } = setup();

    await Promise.all([client.post("/api/v1/thing"), client.post("/api/v1/thing")]);

    expect(server.callsTo("GET", "/api/v1/auth/csrf")).toHaveLength(1);
    expect(server.callsTo("POST", "/api/v1/thing").every((c) => c.headers["x-csrf-token"])).toBe(
      true,
    );
  });

  it("always sends credentials: include and never touches web storage", async () => {
    const { server, client } = setup();
    const setItem = vi.spyOn(Storage.prototype, "setItem");

    await client.loadCsrfToken();
    await client.get("/api/v1/thing");

    expect(server.calls.every((c) => c.credentials === "include")).toBe(true);
    expect(setItem).not.toHaveBeenCalled();
    expect(JSON.stringify(localStorage)).not.toContain(server.csrfToken);
    expect(JSON.stringify(sessionStorage)).not.toContain(server.csrfToken);
  });

  it("uses the configured base URL", async () => {
    const fetchImpl = vi.fn(async () => new Response("{}", { status: 200 }));
    const client = new ApiClient({ baseUrl: "https://api.example.test/", fetchImpl });

    await client.get("/api/v1/x", { query: { status: ["a", "b"], empty: undefined } });

    expect(fetchImpl).toHaveBeenCalledWith(
      "https://api.example.test/api/v1/x?status=a&status=b",
      expect.objectContaining({ credentials: "include" }),
    );
  });
});

describe("ApiClient errors", () => {
  const cases: [number, string, Record<string, unknown>, (e: unknown) => boolean][] = [
    [409, "VERSION_CONFLICT", { expected_version: 1, current_version: 2 }, isVersionConflict],
    [
      409,
      "STALE_REVISION",
      { requested_revision_id: "a", current_revision_id: "b" },
      isStaleRevision,
    ],
    [
      409,
      "PUBLICATION_NOT_EDITABLE",
      { publication_id: 5, publication_status: "scheduled", previous_revision_id: null },
      isPublicationNotEditable,
    ],
    [
      409,
      "INVALID_STATE_TRANSITION",
      { command: "schedule", status: "claimed", allowed_from: ["approved"] },
      isInvalidStateTransition,
    ],
  ];

  it.each(cases)("parses %i %s into a typed error", async (status, code, details, guard) => {
    const server = new FakeApi();
    server.on("GET", "/api/v1/x", apiError(status, code, "msg", details));
    const client = new ApiClient({ fetchImpl: server.fetch });

    const error = await client.get("/api/v1/x").catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect(guard(error)).toBe(true);
    expect((error as ApiError).status).toBe(status);
    expect((error as ApiError).details).toEqual(details);
    expect((error as ApiError).requestId).toBe("req-test");
  });

  it.each([
    [401, "isUnauthorized"],
    [403, "isForbidden"],
    [404, "isNotFound"],
    [422, "isValidation"],
  ] as const)("flags %i", async (status, flag) => {
    const server = new FakeApi();
    server.on("GET", "/api/v1/x", apiError(status, "X"));
    const error = (await new ApiClient({ fetchImpl: server.fetch })
      .get("/api/v1/x")
      .catch((e: unknown) => e)) as ApiError;

    expect(error[flag]).toBe(true);
  });

  it("reads Retry-After on a 503", async () => {
    const server = new FakeApi();
    server.on(
      "GET",
      "/api/v1/x",
      apiError(503, "DEPENDENCY_UNAVAILABLE", "busy", { reason: "content_locked" }, {
        "Retry-After": "2",
      }),
    );

    const error = await new ApiClient({ fetchImpl: server.fetch })
      .get("/api/v1/x")
      .catch((e: unknown) => e);

    expect(isUnavailable(error)).toBe(true);
    expect((error as ApiError).retryAfter).toBe(2);
  });

  it("notifies on 401 but not on a failed login", async () => {
    const server = new FakeApi();
    server
      .on("GET", "/api/v1/x", apiError(401, "AUTH_REQUIRED"))
      .on("POST", "/api/v1/auth/login", apiError(401, "INVALID_CREDENTIALS"));
    const client = new ApiClient({ fetchImpl: server.fetch });
    const listener = vi.fn();
    client.onUnauthorized(listener);

    await client.post("/api/v1/auth/login", {}).catch(() => undefined);
    expect(listener).not.toHaveBeenCalled();

    await client.get("/api/v1/x").catch(() => undefined);
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("turns a non-envelope body and a network failure into ApiError", async () => {
    const htmlClient = new ApiClient({
      fetchImpl: async () => new Response("<html>bad gateway</html>", { status: 502 }),
    });
    const html = (await htmlClient.get("/x").catch((e: unknown) => e)) as ApiError;
    expect(html.code).toBe("DEPENDENCY_UNAVAILABLE");

    const downClient = new ApiClient({
      fetchImpl: async () => {
        throw new TypeError("Failed to fetch");
      },
    });
    const down = (await downClient.get("/x").catch((e: unknown) => e)) as ApiError;
    expect(down.code).toBe("NETWORK_ERROR");
    expect(down.status).toBe(0);
  });
});

describe("parseRetryAfter", () => {
  it("handles seconds, dates and garbage", () => {
    expect(parseRetryAfter("3")).toBe(3);
    expect(parseRetryAfter(null)).toBeNull();
    expect(parseRetryAfter("soon")).toBeNull();
    expect(parseRetryAfter("Wed, 21 Oct 2026 07:28:10 GMT", Date.parse("2026-10-21T07:28:00Z"))).toBe(
      10,
    );
  });
});
