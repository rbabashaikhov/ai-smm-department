import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { apiError, ok } from "../test/fakeApi";
import { PROJECT_B_ID, PROJECT_ID, createServer, createTwoProjectServer, makeMe, makeTopicIdeationJob, page } from "../test/fixtures";
import { currentPath, renderApp } from "../test/render";
import type { ContentIdea, GenerationJob, Role } from "../types/api";

const IDEA_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const JOB_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const T0 = "2026-10-18T10:00:00+00:00";

function makeIdea(status: ContentIdea["status"] = "proposed"): ContentIdea {
  return {
    id: IDEA_ID,
    project_id: PROJECT_ID,
    ideation_job_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    position: 1,
    title: "Осенний запуск",
    description: "Идея для нового поста",
    angle: "Практическая польза",
    status,
    generation_job_id: status === "used" ? JOB_ID : null,
    created_at: T0,
    updated_at: T0,
  };
}

function makeJob(): GenerationJob {
  return {
    id: JOB_ID,
    project_id: PROJECT_ID,
    requested_by_user_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
    status: "queued",
    params: {}, result: {}, last_error: null, attempt_count: 0, max_attempts: 2,
    created_at: T0, updated_at: T0, finished_at: null,
  };
}

function ideasServer(role: Role, ideas: ContentIdea[] = []) {
  return createServer(role).on("GET", "/api/v1/projects/:projectId/content-ideas", (request) =>
    ok(page(ideas, ideas.length, Number(request.query.get("limit") ?? 25), Number(request.query.get("offset") ?? 0))),
  );
}

describe("content ideas page", () => {
  it("shows Generate Topics after owner login returns to the protected ideas URL", async () => {
    let signedIn = false;
    const server = ideasServer("owner").on("GET", "/api/v1/auth/me", () =>
      signedIn ? ok(makeMe("owner")) : apiError(401, "AUTH_REQUIRED"),
    ).on("POST", "/api/v1/auth/login", () => {
      signedIn = true;
      return ok({ user: makeMe("owner").user, csrf_token: "test-csrf" });
    });
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/ideas`);
    await user.type(await screen.findByLabelText("Email"), "operator@example.com");
    await user.type(screen.getByLabelText("Password"), "test-password");
    await user.click(screen.getByRole("button", { name: "Войти" }));

    expect(await screen.findByRole("button", { name: "Generate Topics" })).toBeEnabled();
    expect(currentPath()).toBe(`/projects/${PROJECT_ID}/ideas`);
  });

  it("lets an owner queue topic ideation once and shows its status", async () => {
    const queued = makeTopicIdeationJob({ topic_hint: "Весенние рецепты" });
    const server = ideasServer("owner").on(
      "GET", "/api/v1/projects/:projectId/topic-ideation-jobs", ok(page([queued])),
    ).on(
      "POST", `/api/v1/projects/${PROJECT_ID}/topic-ideation-jobs`, ok(queued, 202),
    );
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/ideas`);

    await user.clear(await screen.findByLabelText("Idea count"));
    await user.type(screen.getByLabelText("Idea count"), "2");
    await user.type(screen.getByLabelText("Topic hint"), "Весенние рецепты");
    await user.click(screen.getByRole("button", { name: "Generate Topics" }));

    await waitFor(() => expect(server.callsTo("POST", /topic-ideation-jobs$/)).toHaveLength(1));
    expect(server.callsTo("POST", /topic-ideation-jobs$/)[0]!.body).toEqual({ idea_count: 2, topic_hint: "Весенние рецепты" });
    expect(screen.getByText("queued")).toBeInTheDocument();
  });

  it("keeps topic creation blocked when verification fails after a lost POST response", async () => {
    const server = ideasServer("owner").on(
      "POST", `/api/v1/projects/${PROJECT_ID}/topic-ideation-jobs`, async () => {
        throw new TypeError("connection lost after sending");
      },
    );
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/ideas`);
    await user.click(await screen.findByRole("button", { name: "Generate Topics" }));
    await screen.findByRole("button", { name: "Проверить задания" });
    server.on("GET", "/api/v1/projects/:projectId/topic-ideation-jobs",
      apiError(500, "INTERNAL_ERROR", "Queue verification failed."));

    await user.click(screen.getByRole("button", { name: "Проверить задания" }));
    await screen.findByText(/Queue verification failed/);
    expect(screen.getByRole("button", { name: "Проверить задания" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Generate Topics" })).not.toBeInTheDocument();
    expect(server.callsTo("POST", /topic-ideation-jobs$/)).toHaveLength(1);

    server.on("GET", "/api/v1/projects/:projectId/topic-ideation-jobs", ok(page([])));
    await user.click(screen.getByRole("button", { name: "Проверить задания" }));
    expect(await screen.findByRole("button", { name: "Generate Topics" })).toBeEnabled();
    expect(server.callsTo("POST", /topic-ideation-jobs$/)).toHaveLength(1);
  });

  it("resets the topic form and uncertain outcome when switching projects", async () => {
    const server = createTwoProjectServer({ roles: { demo: "owner", beta: "owner" } }).on(
      "GET", "/api/v1/projects/:projectId/content-ideas", ok(page([])),
    ).on("GET", "/api/v1/projects/:projectId/topic-ideation-jobs", ok(page([])))
      .on("POST", `/api/v1/projects/${PROJECT_ID}/topic-ideation-jobs`, async () => {
        throw new TypeError("connection lost after sending");
      });
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/ideas`);
    await user.type(await screen.findByLabelText("Topic hint"), "Private topic for project A");
    await user.click(screen.getByRole("button", { name: "Generate Topics" }));
    await screen.findByRole("button", { name: "Проверить задания" });
    await user.selectOptions(screen.getByRole("combobox", { name: "Project switcher" }), PROJECT_B_ID);
    await waitFor(() => expect(currentPath()).toBe(`/projects/${PROJECT_B_ID}/ideas`));

    expect(screen.getByLabelText("Topic hint")).toHaveValue("");
    expect(screen.getByRole("button", { name: "Generate Topics" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Проверить задания" })).not.toBeInTheDocument();
  });

  it("polls active topic jobs and refreshes ideas when they succeed", async () => {
    let complete = false;
    const server = ideasServer("owner").on(
      "GET", "/api/v1/projects/:projectId/topic-ideation-jobs", () =>
        ok(page([makeTopicIdeationJob({ status: complete ? "succeeded" : "queued" })])),
    ).on("GET", "/api/v1/projects/:projectId/content-ideas", () =>
      ok(page(complete ? [makeIdea()] : [])),
    );
    renderApp(server, `/projects/${PROJECT_ID}/ideas`);
    await screen.findByText("queued");
    complete = true;
    expect(await screen.findByText("Осенний запуск", {}, { timeout: 5_000 })).toBeInTheDocument();
    expect(screen.getByText("succeeded")).toBeInTheDocument();
  });

  it("lists idea details, used job link, filters and pagination", async () => {
    const server = ideasServer("viewer", [makeIdea("used")]).on(
      "GET", "/api/v1/projects/:projectId/content-ideas", (request) =>
        ok(page([makeIdea("used")], 30, 25, Number(request.query.get("offset") ?? 0))),
    );
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/ideas`);

    expect(await screen.findByText("Осенний запуск")).toBeInTheDocument();
    expect(screen.getByText("Идея для нового поста")).toBeInTheDocument();
    expect(screen.getByText("Практическая польза")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Generation job" })).toHaveAttribute(
      "href", `/projects/${PROJECT_ID}/generation/${JOB_ID}`,
    );
    await user.selectOptions(screen.getByLabelText("Idea status filter"), "used");
    await waitFor(() => expect(server.callsTo("GET", `/api/v1/projects/${PROJECT_ID}/content-ideas`).at(-1)!.query.getAll("status")).toEqual(["used"]));
    await user.click(screen.getByRole("button", { name: /Вперёд/ }));
    await waitFor(() => expect(server.callsTo("GET", `/api/v1/projects/${PROJECT_ID}/content-ideas`).at(-1)!.query.get("offset")).toBe("25"));
  });

  it("keeps the Ideas section and never displays the previous project's ideas when switching", async () => {
    const betaIdea = { ...makeIdea(), id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", project_id: PROJECT_B_ID, title: "Beta idea" };
    const server = createTwoProjectServer().on(
      "GET", "/api/v1/projects/:projectId/content-ideas", (request) =>
        ok(page(request.params.projectId === PROJECT_B_ID ? [betaIdea] : [makeIdea()])),
    );
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/ideas`);

    expect(await screen.findByText("Осенний запуск")).toBeInTheDocument();
    await user.selectOptions(screen.getByRole("combobox", { name: "Project switcher" }), PROJECT_B_ID);
    await waitFor(() => expect(currentPath()).toBe(`/projects/${PROJECT_B_ID}/ideas`));
    expect(await screen.findByText("Beta idea")).toBeInTheDocument();
    expect(screen.queryByText("Осенний запуск")).not.toBeInTheDocument();
  });

  it.each(["viewer", "editor", "admin"] as const)("does not offer commands to %s", async (role) => {
    renderApp(ideasServer(role, [makeIdea()]), `/projects/${PROJECT_ID}/ideas`);
    await screen.findByText("Осенний запуск");
    expect(screen.queryByRole("button", { name: "Создать черновик" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Отклонить" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Generate Topics" })).not.toBeInTheDocument();
  });

  it("does not offer commands to an owner while mutations are disabled", async () => {
    const server = createServer("owner", { mutationsEnabled: false }).on(
      "GET", "/api/v1/projects/:projectId/content-ideas", ok(page([makeIdea()])),
    );
    renderApp(server, `/projects/${PROJECT_ID}/ideas`);
    await screen.findByText("Осенний запуск");
    expect(screen.queryByRole("button", { name: "Создать черновик" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Отклонить" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Generate Topics" })).not.toBeInTheDocument();
  });

  it("rejects only after confirmation and refreshes the list", async () => {
    const rejected = makeIdea("rejected");
    const server = ideasServer("owner", [makeIdea()]).on(
      "POST", `/api/v1/projects/${PROJECT_ID}/content-ideas/${IDEA_ID}/reject`, ok(rejected),
    );
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/ideas`);
    await user.click(await screen.findByRole("button", { name: "Отклонить" }));
    expect(server.callsTo("POST", /reject$/)).toHaveLength(0);
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Отклонить" }));
    await waitFor(() => expect(server.callsTo("POST", /reject$/)).toHaveLength(1));
    expect(server.callsTo("POST", /reject$/)[0]!.headers["x-csrf-token"]).toBe(server.csrfToken);
  });

  it("uses once, including a replayed response, then opens its generation job", async () => {
    const used = makeIdea("used");
    const server = ideasServer("owner", [makeIdea()]).on(
      "POST", `/api/v1/projects/${PROJECT_ID}/content-ideas/${IDEA_ID}/use`,
      ok({ idea: used, generation_job_id: JOB_ID, generation_job: makeJob(), replayed: true, topic_includes: [] }),
    );
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/ideas`);
    const button = await screen.findByRole("button", { name: "Создать черновик" });
    await user.dblClick(button);
    await waitFor(() => expect(currentPath()).toBe(`/projects/${PROJECT_ID}/generation/${JOB_ID}`));
    expect(server.callsTo("POST", /\/use$/)).toHaveLength(1);
  });

  it("refreshes a lost use response and removes actions when the idea became used", async () => {
    let reads = 0;
    const server = createServer("owner")
      .on("GET", "/api/v1/projects/:projectId/content-ideas", () => {
        reads += 1;
        return ok(page([makeIdea(reads === 1 ? "proposed" : "used")]));
      })
      .on("POST", `/api/v1/projects/${PROJECT_ID}/content-ideas/${IDEA_ID}/use`, async () => {
        throw new TypeError("connection lost");
      });
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/ideas`);

    await user.click(await screen.findByRole("button", { name: "Создать черновик" }));
    expect(await screen.findByRole("link", { name: "Generation job" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Создать черновик" })).not.toBeInTheDocument();
    expect(server.callsTo("POST", /\/use$/)).toHaveLength(1);
  });

  it("re-enables a proposed idea only after a successful reread", async () => {
    const server = createServer("owner")
      .on("GET", "/api/v1/projects/:projectId/content-ideas", ok(page([makeIdea()])))
      .on("POST", `/api/v1/projects/${PROJECT_ID}/content-ideas/${IDEA_ID}/use`, async () => {
        throw new TypeError("connection lost");
      });
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/ideas`);

    await user.click(await screen.findByRole("button", { name: "Создать черновик" }));
    await waitFor(() => expect(server.callsTo("GET", `/api/v1/projects/${PROJECT_ID}/content-ideas`)).toHaveLength(2));
    expect(screen.getByRole("button", { name: "Создать черновик" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Проверить состояние" })).not.toBeInTheDocument();
  });

  it("keeps an idea locked after a failed reread and lets the owner explicitly retry it", async () => {
    let reads = 0;
    const server = createServer("owner")
      .on("GET", "/api/v1/projects/:projectId/content-ideas", () => {
        reads += 1;
        return reads === 1
          ? ok(page([makeIdea()]))
          : apiError(503, "DEPENDENCY_UNAVAILABLE", "Ideas unavailable.");
      })
      .on("POST", `/api/v1/projects/${PROJECT_ID}/content-ideas/${IDEA_ID}/use`, async () => {
        throw new TypeError("connection lost");
      });
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/ideas`);

    await user.click(await screen.findByRole("button", { name: "Создать черновик" }));
    expect(await screen.findByRole("button", { name: "Проверить состояние" }, { timeout: 5_000 })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Создать черновик" })).not.toBeInTheDocument();
    expect(server.callsTo("POST", /\/use$/)).toHaveLength(1);

    await user.click(screen.getByRole("button", { name: "Проверить состояние" }));
    expect(await screen.findByRole("button", { name: "Проверить состояние" }, { timeout: 5_000 })).toBeInTheDocument();
    expect(server.callsTo("POST", /\/use$/)).toHaveLength(1);
  }, 12_000);

  it.each([403, 404, 409, 503])("shows a %i command error without retrying", async (status) => {
    const server = ideasServer("owner", [makeIdea()]).on(
      "POST", `/api/v1/projects/${PROJECT_ID}/content-ideas/${IDEA_ID}/use`,
      apiError(status, "COMMAND_ERROR", "Command refused."),
    );
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/ideas`);
    await user.click(await screen.findByRole("button", { name: "Создать черновик" }));
    expect(await screen.findByText(/Command refused/)).toBeInTheDocument();
    expect(server.callsTo("POST", /\/use$/)).toHaveLength(1);
  });

  it("shows empty and error list states", async () => {
    const empty = ideasServer("viewer");
    const { unmount } = renderApp(empty, `/projects/${PROJECT_ID}/ideas`);
    expect(await screen.findByText("Идей пока нет.")).toBeInTheDocument();
    unmount();
    const failure = ideasServer("viewer").on(
      "GET", "/api/v1/projects/:projectId/content-ideas", apiError(503, "DEPENDENCY_UNAVAILABLE", "Ideas unavailable."),
    );
    renderApp(failure, `/projects/${PROJECT_ID}/ideas`);
    expect(await screen.findByText(/Ideas unavailable/, {}, { timeout: 5_000 })).toBeInTheDocument();
  });
});
