import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { apiError, ok } from "../test/fakeApi";
import { PROJECT_ID, createServer, makeItem, makeRevision, page } from "../test/fixtures";
import { currentPath, renderApp } from "../test/render";
import type { GenerationJob, GenerationJobStatus, Role } from "../types/api";

const JOB_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const ITEM_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const T0 = "2026-10-18T10:00:00+00:00";

function makeJob(
  status: GenerationJobStatus = "queued",
  overrides: Partial<GenerationJob> = {},
): GenerationJob {
  return {
    id: JOB_ID,
    project_id: PROJECT_ID,
    requested_by_user_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    status,
    params: {
      series_size: 3,
      publishing_strategy: "standalone_series",
      topic_hint: "Launch",
    },
    result: {},
    last_error: null,
    attempt_count: status === "queued" ? 0 : 1,
    max_attempts: 2,
    created_at: T0,
    updated_at: T0,
    finished_at: status === "queued" || status === "claimed" ? null : T0,
    ...overrides,
  };
}

function generationServer(role: Role, jobs: GenerationJob[] = []) {
  return createServer(role).on(
    "GET",
    "/api/v1/projects/:projectId/generation-jobs",
    (request) => ok(page(
      jobs,
      jobs.length,
      Number(request.query.get("limit") ?? 25),
      Number(request.query.get("offset") ?? 0),
    )),
  );
}

describe("generation jobs page", () => {
  it("is project-scoped in navigation and lists real jobs", async () => {
    const server = generationServer("viewer", [makeJob()]);
    const { user } = renderApp(server, `/projects/${PROJECT_ID}`);

    await user.click(await screen.findByRole("link", { name: "AI Generation" }));

    await waitFor(() => expect(currentPath()).toBe(`/projects/${PROJECT_ID}/generation`));
    expect(screen.getByRole("heading", { name: "AI Generation" })).toBeInTheDocument();
    expect(screen.getByText("Demo Project", { selector: "strong" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: JOB_ID })).toHaveAttribute(
      "href",
      `/projects/${PROJECT_ID}/generation/${JOB_ID}`,
    );
    expect(screen.queryByRole("form", { name: "Create generation job" })).not.toBeInTheDocument();
  });

  it("owner creates one job with the validated payload and opens its canonical detail", async () => {
    const server = generationServer("owner");
    server.on(
      "POST",
      `/api/v1/projects/${PROJECT_ID}/generation-jobs`,
      ok(makeJob("queued"), 202),
    );
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/generation`);

    await screen.findByRole("form", { name: "Create generation job" });
    await user.type(screen.getByLabelText("Topic hint"), " Product launch ");
    await user.clear(screen.getByLabelText("Series size"));
    await user.type(screen.getByLabelText("Series size"), "2");
    await user.selectOptions(screen.getByLabelText("Publishing strategy"), "reply_thread");
    await user.click(screen.getByRole("button", { name: "Generate" }));

    await waitFor(() =>
      expect(currentPath()).toBe(`/projects/${PROJECT_ID}/generation/${JOB_ID}`),
    );
    const calls = server.callsTo("POST", `/api/v1/projects/${PROJECT_ID}/generation-jobs`);
    expect(calls).toHaveLength(1);
    expect(calls[0]!.body).toEqual({
      series_size: 2,
      publishing_strategy: "reply_thread",
      topic_hint: "Product launch",
    });
    expect(calls[0]!.headers["x-csrf-token"]).toBe(server.csrfToken);
  });

  it.each(["viewer", "editor", "admin"] as const)(
    "%s gets a read-only list and no create form",
    async (role) => {
      const server = generationServer(role);
      renderApp(server, `/projects/${PROJECT_ID}/generation`);

      await screen.findByRole("heading", { name: "AI Generation" });
      expect(screen.queryByRole("form", { name: "Create generation job" })).not.toBeInTheDocument();
      expect(screen.getByText(/только владелец проекта/)).toBeInTheDocument();
    },
  );

  it("hides create on a read-only deployment", async () => {
    const server = createServer("owner", { mutationsEnabled: false }).on(
      "GET",
      "/api/v1/projects/:projectId/generation-jobs",
      ok(page([])),
    );
    renderApp(server, `/projects/${PROJECT_ID}/generation`);

    await screen.findByRole("heading", { name: "AI Generation" });
    expect(screen.queryByRole("form", { name: "Create generation job" })).not.toBeInTheDocument();
    expect(screen.getByText(/Список заданий доступен для просмотра/)).toBeInTheDocument();
  });

  it("sends status and pagination through the list contract", async () => {
    const server = generationServer("viewer", [makeJob()]);
    server.on("GET", "/api/v1/projects/:projectId/generation-jobs", (request) =>
      ok(page(
        [makeJob()],
        30,
        Number(request.query.get("limit") ?? 25),
        Number(request.query.get("offset") ?? 0),
      )),
    );
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/generation`);

    await screen.findByRole("link", { name: JOB_ID });
    await user.selectOptions(screen.getByLabelText("Generation status filter"), "queued");
    await waitFor(() => expect(
      server.callsTo("GET", `/api/v1/projects/${PROJECT_ID}/generation-jobs`).at(-1)!.query.getAll("status"),
    ).toEqual(["queued"]));
    await user.click(screen.getByRole("button", { name: /Вперёд/ }));
    await waitFor(() => expect(
      server.callsTo("GET", `/api/v1/projects/${PROJECT_ID}/generation-jobs`).at(-1)!.query.get("offset"),
    ).toBe("25"));
  });

  it("does not repeat an ambiguous create and tells the owner to check the list", async () => {
    const server = generationServer("owner");
    server.on("POST", `/api/v1/projects/${PROJECT_ID}/generation-jobs`, async () => {
      throw new TypeError("connection lost");
    });
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/generation`);

    await user.click(await screen.findByRole("button", { name: "Generate" }));

    expect(await screen.findByText(/неизвестно, было ли задание создано/)).toBeInTheDocument();
    expect(server.callsTo("POST", `/api/v1/projects/${PROJECT_ID}/generation-jobs`)).toHaveLength(1);
  });

  it("rejects an invalid series size before calling the API", async () => {
    const server = generationServer("owner");
    const { container } = renderApp(server, `/projects/${PROJECT_ID}/generation`);

    await screen.findByRole("form", { name: "Create generation job" });
    fireEvent.change(screen.getByLabelText("Series size"), { target: { value: "6" } });
    fireEvent.submit(container.querySelector('form[aria-label="Create generation job"]')!);

    expect(await screen.findByText(/целым числом от 1 до 5/)).toBeInTheDocument();
    expect(server.callsTo("POST", `/api/v1/projects/${PROJECT_ID}/generation-jobs`)).toHaveLength(0);
  });

  it("shows backend validation details without retrying create", async () => {
    const server = generationServer("owner").on(
      "POST",
      `/api/v1/projects/${PROJECT_ID}/generation-jobs`,
      apiError(422, "VALIDATION_ERROR", "Invalid generation parameters.", {
        fields: [{ field: "topic_hint", rule: "max_length" }],
      }),
    );
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/generation`);

    await user.click(await screen.findByRole("button", { name: "Generate" }));

    expect(await screen.findByText("topic_hint", { selector: "code" })).toBeInTheDocument();
    expect(screen.getByText(/max_length/)).toBeInTheDocument();
    expect(server.callsTo("POST", `/api/v1/projects/${PROJECT_ID}/generation-jobs`)).toHaveLength(1);
  });
});

describe("generation job detail", () => {
  it("restores a succeeded job by URL and links only validated drafts", async () => {
    const job = makeJob("succeeded", {
      result: {
        content_item_ids: [ITEM_ID],
        series_title: "Product series",
        revision_count: 2,
        tokens: { input: 100, output: 50, cached: 10 },
        cost_usd: 0.012345,
        seconds: 4.5,
      },
    });
    const server = generationServer("owner").on(
      "GET",
      "/api/v1/generation-jobs/:jobId",
      ok(job),
    );
    renderApp(server, `/projects/${PROJECT_ID}/generation/${JOB_ID}`);

    expect(await screen.findByRole("heading", { name: "Generation job" })).toBeInTheDocument();
    expect(screen.getByText("Product series")).toBeInTheDocument();
    expect(screen.getByText("100")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: new RegExp(ITEM_ID) })).toHaveAttribute(
      "href",
      `/content/${ITEM_ID}`,
    );
    expect(screen.getByText(/Перед отправкой на согласование/)).toBeInTheDocument();
    expect(server.callsTo("GET", `/api/v1/generation-jobs/${JOB_ID}`)).toHaveLength(1);
    expect(server.calls.filter((call) => call.method === "POST")).toHaveLength(0);
    for (const action of ["Approve", "Submit Review", "Publish", "Schedule"]) {
      expect(screen.queryByRole("button", { name: action })).not.toBeInTheDocument();
    }
  });

  it("shows malformed result safely without draft links", async () => {
    const server = generationServer("owner").on(
      "GET",
      "/api/v1/generation-jobs/:jobId",
      ok(makeJob("succeeded", { result: { content_item_ids: ["not-a-uuid"] } })),
    );
    renderApp(server, `/projects/${PROJECT_ID}/generation/${JOB_ID}`);

    expect(await screen.findByText(/некорректный формат/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Draft/ })).not.toBeInTheDocument();
  });

  it("opens the existing content detail with editor verdict and revision history", async () => {
    const revision = makeRevision({
      content_item_id: ITEM_ID,
      source: "copywriter",
      created_by_user_id: null,
      editor_score: 9,
      editor_notes: "Approved by the agent editor",
      social_account_id: null,
    });
    const item = makeItem("draft", {
      id: ITEM_ID,
      title: "Generated launch draft",
      current_revision_id: revision.id,
      current_revision: revision,
    });
    const server = createServer("owner", { item })
      .on("GET", "/api/v1/projects/:projectId/generation-jobs", ok(page([])))
      .on(
        "GET",
        "/api/v1/generation-jobs/:jobId",
        ok(makeJob("succeeded", { result: { content_item_ids: [ITEM_ID] } })),
      );
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/generation/${JOB_ID}`);

    expect(await screen.findByText(/Перед отправкой на согласование/)).toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: new RegExp(ITEM_ID) }));

    expect(await screen.findByRole("heading", { name: "Generated launch draft" })).toBeInTheDocument();
    expect(screen.getByText("Revision history")).toBeInTheDocument();
    expect(screen.getAllByText("copywriter").length).toBeGreaterThan(0);
    expect(screen.getAllByText("9").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Approved by the agent editor").length).toBeGreaterThan(0);
    expect(server.calls.filter((call) => call.method === "POST")).toHaveLength(0);
  });

  it("shows a failed job's safe error and no cancel action", async () => {
    const server = generationServer("owner").on(
      "GET",
      "/api/v1/generation-jobs/:jobId",
      ok(makeJob("failed", { last_error: "knowledge unavailable" })),
    );
    renderApp(server, `/projects/${PROJECT_ID}/generation/${JOB_ID}`);

    expect(await screen.findByText(/knowledge unavailable/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel generation" })).not.toBeInTheDocument();
  });

  it("shows cancelled as terminal and offers no cancel action", async () => {
    const server = generationServer("owner").on(
      "GET",
      "/api/v1/generation-jobs/:jobId",
      ok(makeJob("cancelled")),
    );
    renderApp(server, `/projects/${PROJECT_ID}/generation/${JOB_ID}`);

    await screen.findByRole("heading", { name: "Generation job" });
    expect(screen.getAllByText("cancelled").length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: "Cancel generation" })).not.toBeInTheDocument();
  });

  it("owner cancels an active job once; the terminal response removes the action", async () => {
    let current = makeJob("claimed");
    const server = generationServer("owner")
      .on("GET", "/api/v1/generation-jobs/:jobId", () => ok(current))
      .on("POST", "/api/v1/generation-jobs/:jobId/cancel", () => {
        current = makeJob("cancelled");
        return ok(current);
      });
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/generation/${JOB_ID}`);

    await user.click(await screen.findByRole("button", { name: "Cancel generation" }));

    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Cancel generation" })).not.toBeInTheDocument(),
    );
    const calls = server.callsTo("POST", `/api/v1/generation-jobs/${JOB_ID}/cancel`);
    expect(calls).toHaveLength(1);
    expect(calls[0]!.headers["x-csrf-token"]).toBe(server.csrfToken);
  });

  it("viewer cannot cancel an active job", async () => {
    const server = generationServer("viewer").on(
      "GET",
      "/api/v1/generation-jobs/:jobId",
      ok(makeJob("claimed")),
    );
    renderApp(server, `/projects/${PROJECT_ID}/generation/${JOB_ID}`);

    await screen.findByRole("heading", { name: "Generation job" });
    expect(screen.queryByRole("button", { name: "Cancel generation" })).not.toBeInTheDocument();
  });

  it("keeps an active job visible when cancel is rejected with 409", async () => {
    const server = generationServer("owner")
      .on("GET", "/api/v1/generation-jobs/:jobId", ok(makeJob("claimed")))
      .on(
        "POST",
        "/api/v1/generation-jobs/:jobId/cancel",
        apiError(409, "INVALID_STATE_TRANSITION", "The job has already finished.", {
          command: "cancel",
          status: "succeeded",
          allowed_from: ["queued", "claimed"],
        }),
      );
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/generation/${JOB_ID}`);

    await user.click(await screen.findByRole("button", { name: "Cancel generation" }));

    expect(await screen.findByText(/already finished/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel generation" })).toBeInTheDocument();
    expect(server.callsTo("POST", `/api/v1/generation-jobs/${JOB_ID}/cancel`)).toHaveLength(1);
  });

  it("honours Retry-After when cancel gets a 503 and does not retry POST", async () => {
    const server = generationServer("owner")
      .on("GET", "/api/v1/generation-jobs/:jobId", ok(makeJob("queued")))
      .on(
        "POST",
        "/api/v1/generation-jobs/:jobId/cancel",
        apiError(
          503,
          "DEPENDENCY_UNAVAILABLE",
          "Generation job is locked.",
          { reason: "generation_locked" },
          { "Retry-After": "2" },
        ),
      );
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/generation/${JOB_ID}`);

    await user.click(await screen.findByRole("button", { name: "Cancel generation" }));

    expect(await screen.findByText(/Сервис занят/)).toBeInTheDocument();
    expect(screen.getByText(/повторить через 2 с/)).toBeInTheDocument();
    expect(server.callsTo("POST", `/api/v1/generation-jobs/${JOB_ID}/cancel`)).toHaveLength(1);
  });

  it.each([
    [403, "FORBIDDEN", /Недостаточно прав/],
    [404, "NOT_FOUND", /Не найдено/],
  ] as const)("renders HTTP %i through the common error UI", async (status, code, message) => {
    const server = generationServer("owner").on(
      "GET",
      "/api/v1/generation-jobs/:jobId",
      apiError(status, code, "Job request failed."),
    );
    renderApp(server, `/projects/${PROJECT_ID}/generation/${JOB_ID}`);

    expect(await screen.findByText(message)).toBeInTheDocument();
  });

  it("returns to login when the job request receives 401", async () => {
    const server = generationServer("owner").on(
      "GET",
      "/api/v1/generation-jobs/:jobId",
      apiError(401, "AUTH_REQUIRED", "Authentication is required."),
    );
    renderApp(server, `/projects/${PROJECT_ID}/generation/${JOB_ID}`);

    await waitFor(() => expect(currentPath()).toBe("/login"));
  });

  it("does not render another project's job under the selected project", async () => {
    const server = generationServer("owner").on(
      "GET",
      "/api/v1/generation-jobs/:jobId",
      ok(makeJob("queued", { project_id: "another-project" })),
    );
    renderApp(server, `/projects/${PROJECT_ID}/generation/${JOB_ID}`);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("не относится к выбранному проекту");
    expect(within(alert).getByRole("link", { name: "К списку заданий" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel generation" })).not.toBeInTheDocument();
  });
});
