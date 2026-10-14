// SMM-025B: creating and configuring projects, accounts and the
// write-only credentials manager. Everything runs against FakeApi; no
// token here is real.

import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { apiError, ok } from "../test/fakeApi";
import {
  ACCOUNT_ID,
  CREDENTIAL_ID,
  ITEM_ID,
  PROJECT_ID,
  createServer,
  makeAccount,
  makeCredential,
  makeItem,
  makeMe,
  makeProject,
  makeRevision,
} from "../test/fixtures";
import { currentPath, renderApp } from "../test/render";
import { suggestProjectId } from "../pages/CreateProjectPage";

const TOKEN = "THQtest-fake-token-0123456789abcdef";

describe("create a project", () => {
  it("suggests an id from a Russian name", () => {
    expect(suggestProjectId("Путевые заметки 2026!")).toBe("putevye-zametki-2026");
    expect(suggestProjectId("  --AI  ")).toBe("ai");
  });

  it("an owner creates a project, /auth/me is refreshed and the project opens", async () => {
    const server = createServer("owner");
    const created = { ...makeProject("owner"), id: "travel-notes", display_name: "Путевые заметки" };
    let meCalls = 0;
    server
      .on("GET", "/api/v1/auth/me", () => {
        meCalls += 1;
        const me = makeMe("owner");

        return ok(
          meCalls > 1
            ? { ...me, projects: [...me.projects, { project_id: "travel-notes", display_name: "Путевые заметки", role: "owner" }] }
            : me,
        );
      })
      .on("POST", "/api/v1/projects", ok(created, 201))
      .on("GET", "/api/v1/projects/travel-notes", ok(created));
    const { user } = renderApp(server, "/projects");

    await user.click(await screen.findByRole("link", { name: "+ Новый проект" }));
    await user.type(await screen.findByLabelText(/Название/), "Путевые заметки");
    expect(screen.getByLabelText(/Идентификатор/)).toHaveValue("putevye-zametki");
    await user.clear(screen.getByLabelText(/Идентификатор/));
    await user.type(screen.getByLabelText(/Идентификатор/), "travel-notes");
    await user.type(screen.getByLabelText("Новая тема"), "путешествия{Enter}");
    await user.type(screen.getByLabelText("Tone of Voice"), "Дружелюбно");
    await user.click(screen.getByRole("button", { name: "Создать" }));

    await waitFor(() => expect(currentPath()).toBe("/projects/travel-notes"));
    const body = server.callsTo("POST", "/api/v1/projects")[0]!.body as Record<string, unknown>;
    expect(body).toMatchObject({
      id: "travel-notes",
      display_name: "Путевые заметки",
      topics: ["путешествия"],
      tone_of_voice: "Дружелюбно",
    });
    // Nothing in the request can name an owner or a role.
    expect(body).not.toHaveProperty("role");
    expect(body).not.toHaveProperty("owner_user_id");
    expect(meCalls).toBeGreaterThan(1);
  });

  it("a taken id is reported on the id field", async () => {
    const server = createServer("owner");
    server.on("POST", "/api/v1/projects", apiError(409, "PROJECT_ALREADY_EXISTS", "exists", { field: "id" }));
    const { user } = renderApp(server, "/projects/new");

    await user.type(await screen.findByLabelText(/Название/), "Demo");
    await user.clear(screen.getByLabelText(/Идентификатор/));
    await user.type(screen.getByLabelText(/Идентификатор/), "demo-two");
    await user.click(screen.getByRole("button", { name: "Создать" }));

    expect(await screen.findByText("Проект с таким идентификатором уже существует.")).toBeInTheDocument();
    expect(currentPath()).toBe("/projects/new");
  });

  it("an invalid id cannot be submitted", async () => {
    const server = createServer("owner");
    const { user } = renderApp(server, "/projects/new");

    await user.type(await screen.findByLabelText(/Название/), "X");
    await user.clear(screen.getByLabelText(/Идентификатор/));
    await user.type(screen.getByLabelText(/Идентификатор/), "-bad");

    expect(screen.getByRole("button", { name: "Создать" })).toBeDisabled();
  });

  it("is not offered to a non-owner or on a read-only deployment", async () => {
    renderApp(createServer("admin"), "/projects");
    await screen.findByText("Demo Project", { selector: "strong" });
    expect(screen.queryByRole("link", { name: "+ Новый проект" })).not.toBeInTheDocument();
  });

  it("is not offered read-only even to an owner", async () => {
    renderApp(createServer("owner", { mutationsEnabled: false }), "/projects/new");

    expect(await screen.findByText(/создание проектов отключено/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Создать" })).not.toBeInTheDocument();
  });
});

describe("project settings", () => {
  it("saves the editorial profile and keeps keys it does not edit", async () => {
    const server = createServer("owner");
    server.on("PATCH", "/api/v1/projects/:projectId/settings", (req) =>
      ok({ project_id: PROJECT_ID, ...(req.body as object), publishing_config: {}, version: 3, updated_at: null }),
    );
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/settings`);

    await user.type(await screen.findByLabelText("Новая тема"), "ml{Enter}");
    const profile = screen.getByRole("form", { name: "Editorial profile form" });
    await user.click(within(profile).getByRole("button", { name: "Сохранить" }));

    await waitFor(() => expect(server.callsTo("PATCH", /\/settings$/)).toHaveLength(1));
    expect(server.callsTo("PATCH", /\/settings$/)[0]!.body).toMatchObject({
      expected_version: 2,
      content_config: { tone: "dry", topics: ["ml"] },
    });
  });

  it("a version conflict offers a re-read instead of overwriting", async () => {
    const server = createServer("owner");
    server.on(
      "PATCH",
      "/api/v1/projects/:projectId",
      apiError(409, "VERSION_CONFLICT", "changed", { expected_version: 1, current_version: 2 }),
    );
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/settings`);

    const record = await screen.findByRole("form", { name: "Project record form" });
    await user.clear(within(record).getByLabelText("Название"));
    await user.type(within(record).getByLabelText("Название"), "Renamed");
    await user.click(within(record).getByRole("button", { name: "Сохранить" }));

    expect(await screen.findByTestId("version-conflict")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Перечитать" }));
    await waitFor(() => expect(screen.queryByTestId("version-conflict")).not.toBeInTheDocument());
  });

  it("is read-only for a viewer", async () => {
    renderApp(createServer("viewer"), `/projects/${PROJECT_ID}/settings`);

    const record = await screen.findByRole("form", { name: "Project record form" });
    expect(within(record).getByLabelText("Название")).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Сохранить" })).not.toBeInTheDocument();
  });
});

describe("accounts", () => {
  it("lists accounts with readiness and lets the owner add one", async () => {
    const server = createServer("owner");
    const unverified = makeAccount({
      id: "acc-2",
      display_name: "Эксперименты",
      external_username: null,
      external_account_id: null,
      credential: null,
      publishing_ready: false,
      not_ready_reason: "account_identity_unknown",
    });
    server
      .on("GET", "/api/v1/projects/:projectId/social-accounts", ok([makeAccount(), unverified]))
      .on("POST", "/api/v1/projects/:projectId/social-accounts", ok(makeAccount({ id: "acc-3" }), 201));
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/accounts`);

    expect(await screen.findByText("готов к публикации")).toBeInTheDocument();
    expect(screen.getByText(/подключение не проверено/)).toBeInTheDocument();
    expect(screen.getByText("@travel_ru")).toBeInTheDocument();

    await user.type(screen.getByLabelText(/Новый аккаунт Threads/), "Третий");
    await user.click(screen.getByRole("button", { name: "+ Аккаунт" }));
    await waitFor(() => expect(server.callsTo("POST", /social-accounts$/)).toHaveLength(1));
    expect(server.callsTo("POST", /social-accounts$/)[0]!.body).toEqual({ platform: "threads", display_name: "Третий" });
  });

  it("disabling warns about scheduled rows and sends the version", async () => {
    const server = createServer("owner");
    server
      .on("GET", "/api/v1/projects/:projectId/social-accounts", ok([makeAccount({ scheduled_publications: 2, version: 4 })]))
      .on("POST", "/api/v1/social-accounts/:id/disable", ok(makeAccount({ status: "disabled" })));
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/accounts`);

    await user.click(await screen.findByRole("button", { name: "Отключить" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/В расписании сейчас: 2/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Отключить" }));

    await waitFor(() => expect(server.callsTo("POST", /\/disable$/)).toHaveLength(1));
    expect(server.callsTo("POST", /\/disable$/)[0]!.body).toEqual({ expected_version: 4 });
  });

  it("an editor sees accounts but cannot manage them", async () => {
    const server = createServer("editor");
    server.on("GET", "/api/v1/projects/:projectId/social-accounts", ok([makeAccount()]));
    renderApp(server, `/projects/${PROJECT_ID}/accounts`);

    expect(await screen.findByText("Личный блог")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Отключить" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "+ Аккаунт" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Учётные данные" })).not.toBeInTheDocument();
  });
});

describe("credentials manager", () => {
  it("sends the token once, clears the field and shows only the fingerprint", async () => {
    const server = createServer("owner");
    server
      .on("GET", "/api/v1/projects/:projectId/social-accounts", ok([makeAccount({ credential: null, publishing_ready: false, not_ready_reason: "no_active_credential" })]))
      .on("POST", "/api/v1/social-accounts/:id/credentials", ok(makeCredential({ fingerprint: "abcdefabcdef" }), 201));
    const { user, queryClient } = renderApp(server, `/projects/${PROJECT_ID}/credentials`);

    const input = await screen.findByLabelText("Токен Личный блог");
    expect(input).toHaveAttribute("type", "password");
    expect(input).toHaveAttribute("autocomplete", "off");
    await user.type(input, TOKEN);
    await user.click(screen.getByRole("button", { name: "Сохранить и проверить" }));

    expect(await screen.findByText("abcdefabcdef")).toBeInTheDocument();
    expect(input).toHaveValue("");
    const sent = server.callsTo("POST", /\/credentials$/)[0]!;
    expect(sent.body).toMatchObject({ access_token: TOKEN, verify: true });
    expect(sent.headers["x-csrf-token"] ?? sent.headers["X-CSRF-Token"]).toBeTruthy();

    // The token is nowhere in the page or in TanStack Query's caches.
    expect(document.body.innerHTML).not.toContain(TOKEN);
    const cached = JSON.stringify([
      queryClient.getQueryCache().getAll().map((q) => q.state.data),
      queryClient.getMutationCache().getAll().map((m) => m.state.variables),
    ]);
    expect(cached).not.toContain(TOKEN);
  });

  it("a token of another account is refused with a clear message and the field is cleared", async () => {
    const server = createServer("owner");
    server
      .on("GET", "/api/v1/projects/:projectId/social-accounts", ok([makeAccount()]))
      .on("POST", "/api/v1/social-accounts/:id/credentials", apiError(409, "CREDENTIAL_ACCOUNT_MISMATCH"));
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/credentials`);

    const input = await screen.findByLabelText("Токен Личный блог");
    await user.type(input, TOKEN);
    await user.click(screen.getByRole("button", { name: "Заменить и проверить" }));

    expect(await screen.findByText(/принадлежит другому аккаунту Threads/)).toBeInTheDocument();
    expect(input).toHaveValue("");
    expect(document.body.innerHTML).not.toContain(TOKEN);
  });

  it("revoking asks for confirmation and names no secret", async () => {
    const server = createServer("owner");
    server
      .on("GET", "/api/v1/projects/:projectId/social-accounts", ok([makeAccount()]))
      .on("POST", "/api/v1/social-account-credentials/:id/revoke", ok(makeCredential({ status: "revoked" })));
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/credentials`);

    await user.click(await screen.findByRole("button", { name: "Отозвать токен" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Отозвать" }));

    await waitFor(() =>
      expect(server.callsTo("POST", `/api/v1/social-account-credentials/${CREDENTIAL_ID}/revoke`)).toHaveLength(1),
    );
  });

  it("is owner-only", async () => {
    const server = createServer("admin");
    server.on("GET", "/api/v1/projects/:projectId/social-accounts", ok([makeAccount()]));
    renderApp(server, `/projects/${PROJECT_ID}/credentials`);

    expect(await screen.findByText(/только владелец проекта/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Токен Личный блог")).not.toBeInTheDocument();
  });
});

describe("destination on content", () => {
  it("the revision form sends the chosen account and the detail shows it", async () => {
    const revision = makeRevision({ social_account_id: ACCOUNT_ID });
    const item = makeItem("draft", { current_revision: revision });
    const server = createServer("owner", { item });
    server
      .on("GET", "/api/v1/projects/:projectId/social-accounts", ok([makeAccount()]))
      .on("POST", "/api/v1/content-items/:id/revisions", (req) =>
        ok({ item, revision: { ...revision, ...(req.body as object) } }, 201),
      );
    const { user } = renderApp(server, `/content/${ITEM_ID}`);

    expect(await screen.findByTestId("destination")).toHaveTextContent("Личный блог (@travel_ru)");

    await user.click(await screen.findByRole("button", { name: /New revision/i }));
    const select = await screen.findByLabelText("Аккаунт публикации");
    expect(select).toHaveValue(ACCOUNT_ID);
    await user.click(screen.getByRole("button", { name: "Save as new revision" }));

    await waitFor(() => expect(server.callsTo("POST", /\/revisions$/)).toHaveLength(1));
    expect(server.callsTo("POST", /\/revisions$/)[0]!.body).toMatchObject({ social_account_id: ACCOUNT_ID });
  });

  it("a project without legacy publishing says a destination is required", async () => {
    const item = makeItem("draft", { current_revision: makeRevision({ social_account_id: null }) });
    renderApp(createServer("editor", { item }), `/content/${ITEM_ID}`);

    expect(await screen.findByTestId("destination")).toHaveTextContent(/не выбран/);
  });
});
