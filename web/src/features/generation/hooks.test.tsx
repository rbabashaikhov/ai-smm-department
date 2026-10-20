import { QueryClient } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { ApiClient } from "../../api/client";
import { ApiProvider } from "../../api/context";
import { createEndpoints } from "../../api/endpoints";
import { ApiError } from "../../api/errors";
import { qk } from "../../api/queryKeys";
import { apiError, FakeApi, ok } from "../../test/fakeApi";
import type { GenerationJob, GenerationJobStatus } from "../../types/api";
import {
  GENERATION_POLL_INTERVAL_MS,
  generationPollingInterval,
  isGenerationOutcomeUnknown,
  useCancelGenerationJob,
  useCreateGenerationJob,
  useGenerationJob,
  useGenerationJobs,
} from "./hooks";
import { parseGenerationResult } from "./result";

const PROJECT = "project-a";
const JOB_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const ITEM_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

function makeJob(status: GenerationJobStatus = "queued"): GenerationJob {
  return {
    id: JOB_ID,
    project_id: PROJECT,
    requested_by_user_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    status,
    params: { series_size: 3, publishing_strategy: null, topic_hint: null },
    result: {},
    last_error: null,
    attempt_count: 0,
    max_attempts: 2,
    created_at: "2026-10-18T10:00:00+00:00",
    updated_at: "2026-10-18T10:00:00+00:00",
    finished_at: null,
  };
}

function setup(server = new FakeApi()) {
  const client = new ApiClient({ fetchImpl: server.fetch });
  client.setCsrfToken(server.csrfToken);
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <ApiProvider client={client} queryClient={queryClient}>
      {children}
    </ApiProvider>
  );

  return { server, client, queryClient, wrapper };
}

describe("generation endpoints", () => {
  it("uses the backend paths, payload, pagination, repeated status filters and CSRF", async () => {
    const { server, client } = setup();
    const job = makeJob();
    server
      .on("POST", `/api/v1/projects/${PROJECT}/generation-jobs`, ok(job, 202))
      .on("GET", `/api/v1/projects/${PROJECT}/generation-jobs`, ok({
        items: [job], total: 1, limit: 25, offset: 50,
      }))
      .on("GET", `/api/v1/generation-jobs/${JOB_ID}`, ok(job))
      .on("POST", `/api/v1/generation-jobs/${JOB_ID}/cancel`, ok(makeJob("cancelled")));
    const api = createEndpoints(client);
    const body = {
      series_size: 2,
      publishing_strategy: "standalone_series" as const,
      topic_hint: "launch",
    };

    await api.generation.create(PROJECT, body);
    await api.generation.list(
      PROJECT,
      { status: ["queued", "claimed"] },
      { limit: 25, offset: 50 },
    );
    await api.generation.get(JOB_ID);
    await api.generation.cancel(JOB_ID);

    const [create] = server.callsTo("POST", `/api/v1/projects/${PROJECT}/generation-jobs`);
    expect(create!.body).toEqual(body);
    expect(create!.headers["x-csrf-token"]).toBe(server.csrfToken);

    const [list] = server.callsTo("GET", `/api/v1/projects/${PROJECT}/generation-jobs`);
    expect(list!.query.get("limit")).toBe("25");
    expect(list!.query.get("offset")).toBe("50");
    expect(list!.query.getAll("status")).toEqual(["queued", "claimed"]);

    const [cancel] = server.callsTo("POST", `/api/v1/generation-jobs/${JOB_ID}/cancel`);
    expect(cancel!.headers["x-csrf-token"]).toBe(server.csrfToken);
  });

  it.each([401, 403, 404, 409, 422, 503])(
    "preserves the common ApiError contract for HTTP %i",
    async (status) => {
      const { server, client } = setup();
      server.on(
        "GET",
        `/api/v1/generation-jobs/${JOB_ID}`,
        apiError(status, status === 503 ? "DEPENDENCY_UNAVAILABLE" : "TEST_ERROR", "failed", {}, {
          ...(status === 503 ? { "Retry-After": "2" } : {}),
        }),
      );

      const error = await createEndpoints(client).generation.get(JOB_ID).catch((value) => value);

      expect(error).toBeInstanceOf(ApiError);
      expect((error as ApiError).status).toBe(status);
      expect((error as ApiError).requestId).toBe("req-test");
      expect((error as ApiError).retryAfter).toBe(status === 503 ? 2 : null);
    },
  );
});

describe("generation query state", () => {
  it("uses stable project-scoped keys", () => {
    const filters = { status: ["queued" as const] };
    const page = { limit: 25, offset: 0 };

    expect(qk.generationJobList("a", filters, page)).not.toEqual(
      qk.generationJobList("b", filters, page),
    );
    expect(qk.generationJob("a", JOB_ID)).not.toEqual(qk.generationJob("b", JOB_ID));
    expect(qk.generationJobList("a", filters, page).slice(0, 3)).toEqual(
      qk.generationJobs("a"),
    );
  });

  it("loads the selected project's filtered page and does nothing without a project", async () => {
    const active = setup();
    active.server.on("GET", `/api/v1/projects/${PROJECT}/generation-jobs`, ok({
      items: [makeJob()], total: 1, limit: 10, offset: 0,
    }));
    const loaded = renderHook(
      () => useGenerationJobs(PROJECT, { status: ["queued"] }, { limit: 10, offset: 0 }),
      { wrapper: active.wrapper },
    );

    await waitFor(() => expect(loaded.result.current.isSuccess).toBe(true));
    expect(active.server.callsTo("GET", `/api/v1/projects/${PROJECT}/generation-jobs`)).toHaveLength(1);

    const absent = setup();
    const idle = renderHook(
      () => useGenerationJobs(undefined, {}, { limit: 10, offset: 0 }),
      { wrapper: absent.wrapper },
    );
    expect(idle.result.current.fetchStatus).toBe("idle");
    expect(absent.server.calls).toHaveLength(0);
  });

  it("polls only active jobs and stops on terminal status or query error", () => {
    expect(generationPollingInterval(makeJob("queued"))).toBe(GENERATION_POLL_INTERVAL_MS);
    expect(generationPollingInterval(makeJob("claimed"))).toBe(GENERATION_POLL_INTERVAL_MS);
    for (const status of ["succeeded", "failed", "cancelled"] as const) {
      expect(generationPollingInterval(makeJob(status))).toBe(false);
    }
    expect(generationPollingInterval(makeJob("queued"), true)).toBe(false);
    expect(generationPollingInterval(undefined)).toBe(false);
  });

  it("does not fetch a detail without both project and job ids", () => {
    const { server, wrapper } = setup();
    const view = renderHook(() => useGenerationJob(PROJECT, undefined), { wrapper });

    expect(view.result.current.fetchStatus).toBe("idle");
    expect(server.calls).toHaveLength(0);
  });
});

describe("generation mutation safety", () => {
  it("coalesces a pending create, never retries it, and invalidates the project list", async () => {
    const { server, queryClient, wrapper } = setup();
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    server.on("POST", `/api/v1/projects/${PROJECT}/generation-jobs`, async () => {
      await gate;
      return ok(makeJob(), 202);
    });
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    const view = renderHook(() => useCreateGenerationJob(PROJECT), { wrapper });
    const body = { series_size: 3 };
    let first!: Promise<GenerationJob>;
    let second!: Promise<GenerationJob>;

    act(() => { first = view.result.current.mutateAsync(body); });
    await waitFor(() => expect(server.callsTo("POST", /generation-jobs$/)).toHaveLength(1));
    act(() => { second = view.result.current.mutateAsync(body); });
    await Promise.resolve();
    expect(server.callsTo("POST", /generation-jobs$/)).toHaveLength(1);

    release();
    await act(async () => { await Promise.all([first, second]); });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: qk.generationJobs(PROJECT) });
    expect(queryClient.getQueryData(qk.generationJob(PROJECT, JOB_ID))).toEqual(makeJob());
  });

  it("does not retry create after a 503", async () => {
    const { server, wrapper } = setup();
    server.on(
      "POST",
      `/api/v1/projects/${PROJECT}/generation-jobs`,
      apiError(503, "DEPENDENCY_UNAVAILABLE"),
    );
    const view = renderHook(() => useCreateGenerationJob(PROJECT), { wrapper });

    await act(async () => {
      await view.result.current.mutateAsync({ series_size: 3 }).catch(() => undefined);
    });

    expect(server.callsTo("POST", /generation-jobs$/)).toHaveLength(1);
  });

  it("updates and invalidates after cancel without retrying", async () => {
    const { server, queryClient, wrapper } = setup();
    server.on("POST", `/api/v1/generation-jobs/${JOB_ID}/cancel`, ok(makeJob("cancelled")));
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    const view = renderHook(() => useCancelGenerationJob(PROJECT, JOB_ID), { wrapper });

    await act(async () => { await view.result.current.mutateAsync(); });

    expect(server.callsTo("POST", `/api/v1/generation-jobs/${JOB_ID}/cancel`)).toHaveLength(1);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: qk.generationJobs(PROJECT) });
    expect(queryClient.getQueryData<GenerationJob>(qk.generationJob(PROJECT, JOB_ID))?.status)
      .toBe("cancelled");
  });

  it("does not retry cancel after a 503", async () => {
    const { server, wrapper } = setup();
    server.on(
      "POST",
      `/api/v1/generation-jobs/${JOB_ID}/cancel`,
      apiError(503, "DEPENDENCY_UNAVAILABLE"),
    );
    const view = renderHook(() => useCancelGenerationJob(PROJECT, JOB_ID), { wrapper });

    await act(async () => {
      await view.result.current.mutateAsync().catch(() => undefined);
    });

    expect(server.callsTo("POST", `/api/v1/generation-jobs/${JOB_ID}/cancel`)).toHaveLength(1);
  });

  it("marks network failures as an ambiguous create outcome", () => {
    expect(isGenerationOutcomeUnknown(new ApiError({
      status: 0,
      code: "NETWORK_ERROR",
      message: "down",
    }))).toBe(true);
    expect(isGenerationOutcomeUnknown(new ApiError({
      status: 503,
      code: "DEPENDENCY_UNAVAILABLE",
      message: "busy",
    }))).toBe(false);
  });
});

describe("generation result validation", () => {
  it("accepts UUID draft ids and typed usage fields", () => {
    expect(parseGenerationResult({
      content_item_ids: [ITEM_ID],
      series_title: "Series",
      revision_count: 2,
      tokens: { input: 10, output: 20, cached: 3 },
      cost_usd: 0.01,
      seconds: 4.5,
    })).toEqual({
      contentItemIds: [ITEM_ID],
      seriesTitle: "Series",
      revisionCount: 2,
      tokens: { input: 10, output: 20, cached: 3 },
      costUsd: 0.01,
      seconds: 4.5,
    });
  });

  it.each([
    {},
    { content_item_ids: "not-an-array" },
    { content_item_ids: [ITEM_ID, "not-a-uuid"] },
    { content_item_ids: [42] },
  ])("returns no draft links for absent or malformed ids: %j", (result) => {
    expect(parseGenerationResult(result).contentItemIds).toBeNull();
  });
});
