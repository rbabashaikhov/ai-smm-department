// SMM-025A: several projects in one Control Center. The journeys J1-J9 and
// the acceptance criteria AC-A1..AC-A7 of
// docs/architecture/smm-025a-product-ui-design.md. Every test also checks
// that nothing but GET (and the login POST) was sent: the switcher and the
// project scope add no write path (AC-A8).

import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { type FakeApi, apiError, ok } from "../test/fakeApi";
import {
  ITEM_B_ID,
  ITEM_ID,
  PROJECT_B_ID,
  PROJECT_ID,
  PUBLICATION_B_ID,
  PUBLICATION_ID,
  createServer,
  createTwoProjectServer,
  deferred,
  makeBetaProject,
  makeItem,
  makeMe,
  makeProject,
  makePublication,
  makeSummary,
  page,
} from "../test/fixtures";
import { currentPath, navigateTo, renderApp } from "../test/render";

const switcher = () => screen.getByRole("combobox", { name: "Project switcher" });

const expectNoWrites = (server: FakeApi) =>
  expect(
    server.calls
      .filter((c) => c.method !== "GET" && c.path !== "/api/v1/auth/login")
      .map((c) => `${c.method} ${c.path}`),
  ).toEqual([]);

/** Requests that reached a project's sub-resources (anything below /projects/{id}/). */
const subResourceCalls = (server: FakeApi, projectId: string) =>
  server.calls.filter((c) => c.path.startsWith(`/api/v1/projects/${projectId}/`));

describe("J1: a user with no project", () => {
  it("lands on an empty Projects page, with the switcher disabled and no project request", async () => {
    const server = createServer("owner");
    server
      .on("GET", "/api/v1/auth/me", ok({ ...makeMe("owner"), projects: [] }))
      .on("GET", "/api/v1/projects", ok([]));
    renderApp(server, "/");

    expect(await screen.findByText(/У вас нет доступа ни к одному проекту/)).toBeInTheDocument();
    expect(currentPath()).toBe("/projects");
    expect(switcher()).toBeDisabled();
    expect(server.calls.filter((c) => /^\/api\/v1\/projects\/./.test(c.path))).toEqual([]);
    expectNoWrites(server);
  });
});

describe("J2: a user with one project", () => {
  it("goes straight to that project's dashboard", async () => {
    const server = createServer("owner");
    renderApp(server, "/");

    expect(await screen.findByRole("heading", { name: "Demo Project" })).toBeInTheDocument();
    expect(currentPath()).toBe(`/projects/${PROJECT_ID}`);
    expectNoWrites(server);
  });
});

describe("J3: a user with several projects", () => {
  it("lands on the overview with each project's queue counters", async () => {
    const server = createTwoProjectServer();
    server.on("GET", "/api/v1/projects/:projectId/operations/summary", (req) =>
      ok(
        req.params.projectId === PROJECT_B_ID
          ? makeSummary({ project_id: PROJECT_B_ID, needs_attention: 0, due_now: 5, published_last_24h: 9 })
          : makeSummary(),
      ),
    );
    renderApp(server, "/");

    const demoRow = (await screen.findByText("Demo Project", { selector: "strong" })).closest("tr")!;
    const betaRow = screen.getByText("Beta Project", { selector: "strong" }).closest("tr")!;
    expect(currentPath()).toBe("/projects");

    // Attention, Due now, Next scheduled, Published 24h.
    await waitFor(() => expect(within(demoRow).getByRole("link", { name: "2" })).toBeInTheDocument());
    expect(within(demoRow).getByRole("link", { name: "2" })).toHaveAttribute(
      "href",
      `/projects/${PROJECT_ID}/attention`,
    );
    expect(within(demoRow).getByText("4")).toBeInTheDocument();
    await waitFor(() => expect(within(betaRow).getByText("9")).toBeInTheDocument());
    expect(within(betaRow).getByText("5")).toBeInTheDocument();
    // The next run is shown in each project's own time zone.
    expect(within(demoRow).getByText(/UTC\+03:00/)).toBeInTheDocument();
    expect(within(betaRow).getByText(/UTC\+02:00/)).toBeInTheDocument();
    expectNoWrites(server);
  });

  it("AC-A4: login with several projects lands on the overview", async () => {
    const server = createTwoProjectServer();
    const signedOut = apiError(401, "AUTH_REQUIRED");
    const anonymous = () => server.callsTo("POST", "/api/v1/auth/login").length === 0;
    const me = { ...makeMe("owner"), projects: [
      { project_id: PROJECT_ID, display_name: "Demo Project", role: "owner" as const },
      { project_id: PROJECT_B_ID, display_name: "Beta Project", role: "viewer" as const },
    ] };
    server
      .on("GET", "/api/v1/auth/me", () => (anonymous() ? signedOut : ok(me)))
      .on("GET", "/api/v1/auth/csrf", () =>
        anonymous() ? signedOut : ok({ csrf_token: server.csrfToken, header_name: "X-CSRF-Token" }),
      );
    const { user } = renderApp(server, "/login");

    await user.type(await screen.findByLabelText("Email"), "operator@example.com");
    await user.type(screen.getByLabelText("Password"), "pw");
    await user.click(screen.getByRole("button", { name: "Войти" }));

    expect(await screen.findByRole("heading", { name: "Проекты" })).toBeInTheDocument();
    expect(currentPath()).toBe("/projects");
  });

  it("AC-A5: one project's failing summary shows '—' on its row only", async () => {
    const server = createTwoProjectServer();
    server.on("GET", "/api/v1/projects/:projectId/operations/summary", (req) =>
      req.params.projectId === PROJECT_B_ID ? apiError(500, "INTERNAL_ERROR") : ok(makeSummary()),
    );
    renderApp(server, "/projects");

    const demoRow = (await screen.findByText("Demo Project", { selector: "strong" })).closest("tr")!;
    const betaRow = screen.getByText("Beta Project", { selector: "strong" }).closest("tr")!;

    await waitFor(() =>
      expect(within(betaRow).getAllByTestId("summary-unavailable").map((td) => td.textContent)).toEqual([
        "—",
        "—",
        "—",
        "—",
      ]),
    );
    expect(await within(demoRow).findByRole("link", { name: "2" })).toBeInTheDocument();
    expect(within(demoRow).queryByTestId("summary-unavailable")).not.toBeInTheDocument();
    // The page itself carries no error banner for one row's failure.
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("J4 / AC-A1 / AC-A2: switching project from a filtered list", () => {
  it("opens the same section of the other project, drops the filters and never shows the first project's rows", async () => {
    const server = createTwoProjectServer();
    const betaProject = deferred<void>();
    const betaRows = deferred<void>();
    // Hold back B's project and B's rows; A's answers stay immediate.
    server
      .on("GET", "/api/v1/projects/:projectId", async (req) => {
        if (req.params.projectId !== PROJECT_B_ID) return ok(makeProject("owner"));

        await betaProject.promise;

        return ok(makeBetaProject("viewer"));
      })
      .on("GET", "/api/v1/projects/:projectId/publications", async (req) => {
        if (req.params.projectId === PROJECT_B_ID) {
          await betaRows.promise;

          return ok(
            page([makePublication("failed", { id: PUBLICATION_B_ID, project_id: PROJECT_B_ID, title: "Beta snapshot" })]),
          );
        }

        return ok(page([makePublication("failed", { title: "Demo snapshot" })]));
      });
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/publications?status=failed`);

    expect(await screen.findByRole("link", { name: "Demo snapshot" })).toBeInTheDocument();

    await user.selectOptions(switcher(), PROJECT_B_ID);

    // Same section, no query string.
    expect(currentPath()).toBe(`/projects/${PROJECT_B_ID}/publications`);
    // While B's project is still loading, nothing of A is on screen.
    expect(screen.queryByRole("link", { name: "Demo snapshot" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("project-strip")).not.toBeInTheDocument();

    betaProject.resolve();
    expect(await screen.findByTestId("project-strip")).toHaveTextContent("Beta Project");
    // B's project is known, B's rows are not: still nothing of A.
    expect(screen.queryByRole("link", { name: "Demo snapshot" })).not.toBeInTheDocument();
    const betaRequest = server.callsTo("GET", `/api/v1/projects/${PROJECT_B_ID}/publications`).at(-1)!;
    expect(betaRequest.query.get("status")).toBeNull();
    expect(betaRequest.query.get("offset")).toBe("0");

    betaRows.resolve();
    expect(await screen.findByRole("link", { name: "Beta snapshot" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Demo snapshot" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Status filter")).toHaveValue("");
    expectNoWrites(server);
  });
});

describe("J5 / AC-A2: switching from a page that has no counterpart", () => {
  it.each([
    ["a content item", `/content/${ITEM_ID}`, "Demo post"],
    ["a publication", `/publications/${PUBLICATION_ID}`, /Demo snapshot/],
    ["the new-content form", `/projects/${PROJECT_ID}/content/new`, "Create Content"],
  ])("from %s it goes to the other project's dashboard", async (_label, path, marker) => {
    const server = createTwoProjectServer();
    const { user } = renderApp(server, path);

    await screen.findByRole("heading", { name: marker });
    await waitFor(() => expect(switcher()).toHaveValue(PROJECT_ID));

    await user.selectOptions(switcher(), PROJECT_B_ID);

    expect(currentPath()).toBe(`/projects/${PROJECT_B_ID}`);
    expect(await screen.findByRole("heading", { name: "Beta Project" })).toBeInTheDocument();
    expectNoWrites(server);
  });
});

describe("J6 / AC-A2: switching from Audit", () => {
  it("goes to the dashboard when the role in the other project cannot read audit", async () => {
    const server = createTwoProjectServer({ roles: { demo: "admin", beta: "viewer" } });
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/audit`);

    await screen.findByRole("heading", { name: "Audit" });
    await user.selectOptions(switcher(), PROJECT_B_ID);

    expect(currentPath()).toBe(`/projects/${PROJECT_B_ID}`);
    await screen.findByRole("heading", { name: "Beta Project" });
    const nav = screen.getByRole("navigation");
    expect(within(nav).queryByRole("link", { name: "Audit" })).not.toBeInTheDocument();
    expect(server.callsTo("GET", `/api/v1/projects/${PROJECT_B_ID}/audit`)).toHaveLength(0);
    expectNoWrites(server);
  });

  it("stays on Audit when the role allows it, with the previous project's filter gone", async () => {
    const server = createTwoProjectServer({ roles: { demo: "owner", beta: "admin" } });
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/audit`);

    await screen.findByRole("heading", { name: "Audit" });
    await user.type(screen.getByLabelText("Action filter"), "content.approved");
    await user.click(screen.getByRole("button", { name: "Применить" }));
    await waitFor(() =>
      expect(
        server.callsTo("GET", `/api/v1/projects/${PROJECT_ID}/audit`).at(-1)!.query.get("action"),
      ).toBe("content.approved"),
    );

    await user.selectOptions(switcher(), PROJECT_B_ID);

    expect(currentPath()).toBe(`/projects/${PROJECT_B_ID}/audit`);
    await waitFor(() =>
      expect(server.callsTo("GET", `/api/v1/projects/${PROJECT_B_ID}/audit`)).toHaveLength(1),
    );
    expect(server.callsTo("GET", `/api/v1/projects/${PROJECT_B_ID}/audit`)[0]!.query.get("action")).toBeNull();
    expect(await screen.findByLabelText("Action filter")).toHaveValue("");
    expectNoWrites(server);
  });
});

describe("J7 / AC-A3: an unknown or forbidden project", () => {
  it("shows one no-access notice and asks for none of its sub-resources", async () => {
    const server = createTwoProjectServer();
    renderApp(server, "/projects/nope");

    expect(await screen.findByText("Проект не найден или у вас нет доступа.")).toBeInTheDocument();
    expect(screen.getAllByTestId("project-no-access")).toHaveLength(1);
    expect(screen.getByRole("link", { name: "← К списку проектов" })).toHaveAttribute("href", "/projects");
    // No second "not found" banner from a page below the scope.
    expect(screen.queryByText("Не найдено")).not.toBeInTheDocument();

    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(server.callsTo("GET", "/api/v1/projects/nope")).toHaveLength(1);
    expect(subResourceCalls(server, "nope")).toEqual([]);
    // The sidebar does not adopt the project.
    const nav = screen.getByRole("navigation");
    expect(within(nav).queryByRole("link", { name: "Dashboard" })).not.toBeInTheDocument();
    expect(switcher()).toHaveValue("");
    expectNoWrites(server);
  });

  it("keeps the current project in the sidebar when another project's URL is refused", async () => {
    const server = createTwoProjectServer();
    renderApp(server, `/projects/${PROJECT_ID}/content`);
    await screen.findByRole("link", { name: "Demo post" });

    await navigateTo("/projects/secret/content");

    expect(await screen.findByTestId("project-no-access")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Demo post" })).not.toBeInTheDocument();
    expect(subResourceCalls(server, "secret")).toEqual([]);
    const nav = screen.getByRole("navigation");
    expect(within(nav).getByRole("link", { name: "Dashboard" })).toHaveAttribute(
      "href",
      `/projects/${PROJECT_ID}`,
    );
    expect(switcher()).toHaveValue(PROJECT_ID);
  });
});

describe("J8 / AC-A7: an empty project on a read-only deployment", () => {
  const emptyBeta = () => {
    const server = createTwoProjectServer({ mutationsEnabled: false, roles: { demo: "owner", beta: "owner" } });
    server
      .on("GET", "/api/v1/projects/:projectId/content-items", (req) =>
        ok(req.params.projectId === PROJECT_B_ID ? page([]) : page([makeItem()])),
      )
      .on("GET", "/api/v1/projects/:projectId/publications", (req) =>
        ok(req.params.projectId === PROJECT_B_ID ? page([]) : page([makePublication()])),
      )
      .on("GET", "/api/v1/projects/:projectId/operations/summary", (req) =>
        ok(
          req.params.projectId === PROJECT_B_ID
            ? makeSummary({
                project_id: PROJECT_B_ID,
                total: 0,
                by_status: {},
                due_now: 0,
                needs_attention: 0,
                published_last_24h: 0,
                api_mutations_enabled: false,
              })
            : makeSummary(),
        ),
      );

    return server;
  };

  it("says the project is empty, on the dashboard and in Content, and offers no create action", async () => {
    const server = emptyBeta();
    renderApp(server, `/projects/${PROJECT_B_ID}`);

    expect(await screen.findByText("В проекте пока нет материалов и публикаций.")).toBeInTheDocument();

    await navigateTo(`/projects/${PROJECT_B_ID}/content`);
    expect(await screen.findByText("В проекте пока нет материалов.")).toBeInTheDocument();
    expect(screen.queryByText(/Ничего не найдено по выбранным фильтрам/)).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Create Content" })).not.toBeInTheDocument();

    await navigateTo(`/projects/${PROJECT_B_ID}/publications`);
    expect(await screen.findByText("В проекте пока нет публикаций.")).toBeInTheDocument();
    expectNoWrites(server);
  });

  it("tells a filtered empty list apart, and its reset drops the filter", async () => {
    const server = emptyBeta();
    const { user } = renderApp(server, `/projects/${PROJECT_B_ID}/content?status=draft`);

    expect(await screen.findByText(/Ничего не найдено по выбранным фильтрам/)).toBeInTheDocument();
    expect(screen.queryByText("В проекте пока нет материалов.")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Сбросить фильтры" }));

    expect(currentPath()).toBe(`/projects/${PROJECT_B_ID}/content`);
    expect(await screen.findByText("В проекте пока нет материалов.")).toBeInTheDocument();
    expect(
      server.callsTo("GET", `/api/v1/projects/${PROJECT_B_ID}/content-items`).at(-1)!.query.get("status"),
    ).toBeNull();
    expectNoWrites(server);
  });
});

describe("J9 / AC-A1: back and forward between two projects", () => {
  it("shows only the current project's rows, even while its answer is delayed, and resets local paging", async () => {
    const server = createTwoProjectServer();
    const betaAttention = deferred<void>();
    server.on("GET", "/api/v1/projects/:projectId/operations/attention", async (req) => {
      if (req.params.projectId === PROJECT_B_ID) {
        await betaAttention.promise;

        return ok(
          page(
            [makePublication("failed", { id: PUBLICATION_B_ID, project_id: PROJECT_B_ID, title: "Beta failure" })],
            60,
            25,
            Number(req.query.get("offset")),
          ),
        );
      }

      return ok(page([makePublication("failed", { title: "Demo failure" })], 60, 25, Number(req.query.get("offset"))));
    });
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/attention`);

    expect(await screen.findByRole("link", { name: "Demo failure" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Вперёд →" }));
    expect(await screen.findByText("26–50 из 60")).toBeInTheDocument();
    expect(
      server.callsTo("GET", `/api/v1/projects/${PROJECT_ID}/operations/attention`).at(-1)!.query.get("offset"),
    ).toBe("25");

    await navigateTo(`/projects/${PROJECT_B_ID}/attention`);
    await screen.findByTestId("project-strip");
    expect(screen.getByTestId("project-strip")).toHaveTextContent("Beta Project");
    // B's rows are held back: A's page must not stand in for them.
    await waitFor(() =>
      expect(server.callsTo("GET", `/api/v1/projects/${PROJECT_B_ID}/operations/attention`)).toHaveLength(1),
    );
    expect(screen.queryByRole("link", { name: "Demo failure" })).not.toBeInTheDocument();
    // Paging starts from the first page in B.
    expect(
      server.callsTo("GET", `/api/v1/projects/${PROJECT_B_ID}/operations/attention`)[0]!.query.get("offset"),
    ).toBe("0");

    betaAttention.resolve();
    expect(await screen.findByRole("link", { name: "Beta failure" })).toBeInTheDocument();
    expect(screen.getByText("1–25 из 60")).toBeInTheDocument();
    // Page forward in B too, so that B's paging cannot leak back into A.
    await user.click(screen.getByRole("button", { name: "Вперёд →" }));
    expect(await screen.findByText("26–50 из 60")).toBeInTheDocument();

    // Both projects are now known: going back renders A at once, without
    // the loading step in between -- the page must still start afresh.
    await navigateTo(-1);
    expect(currentPath()).toBe(`/projects/${PROJECT_ID}/attention`);
    expect(await screen.findByRole("link", { name: "Demo failure" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Beta failure" })).not.toBeInTheDocument();
    // Back in A, local paging starts over too (the first page, still cached).
    expect(screen.getByText("1–25 из 60")).toBeInTheDocument();

    await navigateTo(1);
    expect(currentPath()).toBe(`/projects/${PROJECT_B_ID}/attention`);
    expect(await screen.findByRole("link", { name: "Beta failure" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Demo failure" })).not.toBeInTheDocument();
    expectNoWrites(server);
  });

  it("does not show the previous project's content rows while the next project's list is delayed", async () => {
    const server = createTwoProjectServer();
    const betaContent = deferred<void>();
    server.on("GET", "/api/v1/projects/:projectId/content-items", async (req) => {
      if (req.params.projectId === PROJECT_B_ID) {
        await betaContent.promise;

        return ok(page([makeItem("draft", { id: ITEM_B_ID, project_id: PROJECT_B_ID, title: "Beta post" })]));
      }

      return ok(page([makeItem("draft", { title: "Demo post" })]));
    });
    renderApp(server, `/projects/${PROJECT_ID}/content`);
    expect(await screen.findByRole("link", { name: "Demo post" })).toBeInTheDocument();

    await navigateTo(`/projects/${PROJECT_B_ID}/content`);
    await screen.findByTestId("project-strip");
    await waitFor(() =>
      expect(server.callsTo("GET", `/api/v1/projects/${PROJECT_B_ID}/content-items`)).toHaveLength(1),
    );
    expect(screen.queryByRole("link", { name: "Demo post" })).not.toBeInTheDocument();

    betaContent.resolve();
    expect(await screen.findByRole("link", { name: "Beta post" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Demo post" })).not.toBeInTheDocument();
    expectNoWrites(server);
  });
});

describe("AC-A1: switching to a project that is already loaded", () => {
  it("does not keep the previous project's rows while the list of the next one is delayed", async () => {
    const server = createTwoProjectServer();
    const demoContent = deferred<void>();
    server.on("GET", "/api/v1/projects/:projectId/content-items", async (req) => {
      if (req.params.projectId === PROJECT_ID && req.query.get("limit") === "25") {
        await demoContent.promise;

        return ok(page([makeItem("draft", { title: "Demo post" })]));
      }

      return ok(
        req.params.projectId === PROJECT_B_ID
          ? page([makeItem("draft", { id: ITEM_B_ID, project_id: PROJECT_B_ID, title: "Beta post" })])
          : page([], 0, 1),
      );
    });
    // Demo Project is loaded first (its dashboard), then Beta's content.
    renderApp(server, `/projects/${PROJECT_ID}`);
    await screen.findByRole("heading", { name: "Demo Project" });
    await navigateTo(`/projects/${PROJECT_B_ID}/content`);
    expect(await screen.findByRole("link", { name: "Beta post" })).toBeInTheDocument();

    // Demo's project is cached, so no loading step happens in between;
    // Demo's content list is held back.
    await navigateTo(`/projects/${PROJECT_ID}/content`);
    await waitFor(() => expect(screen.getByTestId("project-strip")).toHaveTextContent("Demo Project"));
    await waitFor(() =>
      expect(
        server.callsTo("GET", `/api/v1/projects/${PROJECT_ID}/content-items`).filter((c) => c.query.get("limit") === "25"),
      ).toHaveLength(1),
    );
    expect(screen.queryByRole("link", { name: "Beta post" })).not.toBeInTheDocument();

    demoContent.resolve();
    expect(await screen.findByRole("link", { name: "Demo post" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Beta post" })).not.toBeInTheDocument();
    expectNoWrites(server);
  });
});

describe("AC-A6: an inactive project", () => {
  it("is labelled in the switcher, the overview and the project strip", async () => {
    const server = createTwoProjectServer({ betaActive: false });
    renderApp(server, "/projects");

    const betaRow = (await screen.findByText("Beta Project", { selector: "strong" })).closest("tr")!;
    expect(within(betaRow).getByText("неактивен")).toBeInTheDocument();
    const demoRow = screen.getByText("Demo Project", { selector: "strong" }).closest("tr")!;
    expect(within(demoRow).queryByText("неактивен")).not.toBeInTheDocument();

    await waitFor(() =>
      expect(within(switcher()).getByRole("option", { name: /Beta Project/ })).toHaveTextContent(
        "Beta Project (viewer) (неактивен)",
      ),
    );
    expect(within(switcher()).getByRole("option", { name: /Demo Project/ })).not.toHaveTextContent("неактивен");

    await navigateTo(`/projects/${PROJECT_B_ID}`);
    expect(await screen.findByTestId("project-strip")).toHaveTextContent("неактивен");

    await navigateTo(`/projects/${PROJECT_ID}`);
    await waitFor(() => expect(screen.getByTestId("project-strip")).toHaveTextContent("Demo Project"));
    expect(screen.getByTestId("project-strip")).not.toHaveTextContent("неактивен");
  });
});

describe("F6: the worker mode is deployment-wide", () => {
  it("says so on every project's dashboard", async () => {
    const server = createTwoProjectServer();
    renderApp(server, `/projects/${PROJECT_B_ID}`);

    expect(await screen.findByTestId("worker-mode-scope")).toHaveTextContent(
      "Режим worker общий для всего деплоя, а не для этого проекта.",
    );
  });
});

describe("F7: breadcrumbs on detail pages", () => {
  it("names the content item's own project", async () => {
    const server = createTwoProjectServer();
    renderApp(server, `/content/${ITEM_B_ID}`);

    const crumbs = await screen.findByTestId("breadcrumbs");
    await waitFor(() => expect(within(crumbs).getByRole("link", { name: "Beta Project" })).toBeInTheDocument());
    expect(within(crumbs).getByRole("link", { name: "Проекты" })).toHaveAttribute("href", "/projects");
    expect(within(crumbs).getByRole("link", { name: "Beta Project" })).toHaveAttribute(
      "href",
      `/projects/${PROJECT_B_ID}`,
    );
    expect(within(crumbs).getByRole("link", { name: "Content" })).toHaveAttribute(
      "href",
      `/projects/${PROJECT_B_ID}/content`,
    );
    expect(crumbs).toHaveTextContent("Beta post");
    await waitFor(() => expect(switcher()).toHaveValue(PROJECT_B_ID));
  });

  it("names the publication's own project", async () => {
    const server = createTwoProjectServer();
    renderApp(server, `/publications/${PUBLICATION_B_ID}`);

    const crumbs = await screen.findByTestId("breadcrumbs");
    await waitFor(() => expect(within(crumbs).getByRole("link", { name: "Beta Project" })).toBeInTheDocument());
    expect(within(crumbs).getByRole("link", { name: "Publications" })).toHaveAttribute(
      "href",
      `/projects/${PROJECT_B_ID}/publications`,
    );
    expect(crumbs).toHaveTextContent(`#${PUBLICATION_B_ID}`);
  });

  it("shows no project when the row cannot be loaded", async () => {
    const server = createTwoProjectServer();
    server.on("GET", "/api/v1/content-items/:id", apiError(404, "NOT_FOUND", "Content item not found."));
    renderApp(server, `/content/${ITEM_ID}`);

    expect(await screen.findByText("Content item not found.")).toBeInTheDocument();
    expect(screen.queryByTestId("breadcrumbs")).not.toBeInTheDocument();
  });
});
