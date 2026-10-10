// A read-only deployment (AI_SMM_API_MUTATIONS_ENABLED=false) and the
// worker-mode panel. The server is the authority on both: these tests check
// that the UI neither offers what the server will refuse nor claims a
// worker mode the server did not report.

import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { apiError, ok } from "../test/fakeApi";
import {
  ITEM_ID,
  PROJECT_ID,
  PUBLICATION_ID,
  createServer,
  makeItem,
  makePublication,
  makeSummary,
  page,
} from "../test/fixtures";
import type { ContentStatus, PublicationStatus, Role } from "../types/api";
import { renderApp } from "../test/render";

const WRITE_BUTTONS = [
  "New Revision",
  "Submit Review",
  "Approve",
  "Reject",
  "Подготовить публикацию",
  "Schedule",
  "Reschedule",
  "Cancel",
];

const ROLES: Role[] = ["viewer", "editor", "admin", "owner"];

describe("read-only deployment", () => {
  it.each(ROLES)("%s sees the read-only notice", async (role) => {
    const server = createServer(role, { mutationsEnabled: false });
    renderApp(server, `/projects/${PROJECT_ID}`);

    expect(await screen.findByTestId("read-only-banner")).toHaveTextContent("Только чтение");
  });

  it.each<ContentStatus>(["draft", "in_review", "approved"])(
    "owner is offered no content action in %s",
    async (status) => {
      const server = createServer("owner", { item: makeItem(status), mutationsEnabled: false });
      renderApp(server, `/content/${ITEM_ID}`);

      await screen.findByRole("heading", { name: "Launch post" });

      for (const name of WRITE_BUTTONS) {
        expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
      }
    },
  );

  it.each<PublicationStatus>(["approved", "scheduled", "failed", "draft"])(
    "owner is offered no queue command for a %s publication",
    async (status) => {
      const server = createServer("owner", {
        publication: makePublication(status),
        mutationsEnabled: false,
      });
      renderApp(server, `/publications/${PUBLICATION_ID}`);

      await screen.findByRole("heading", { name: /Snapshot title/ });

      for (const name of ["Schedule", "Reschedule", "Cancel"]) {
        expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
      }
    },
  );

  it("owner sees no queue command in the publication list", async () => {
    const server = createServer("owner", { mutationsEnabled: false });
    server.on(
      "GET",
      "/api/v1/projects/:projectId/publications",
      ok(page([makePublication("approved"), makePublication("scheduled", { id: 2, title: "Queued" })])),
    );
    renderApp(server, `/projects/${PROJECT_ID}/publications`);

    await screen.findByRole("link", { name: "Snapshot title" });

    for (const name of ["Schedule", "Reschedule", "Cancel"]) {
      expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
    }
  });

  it("owner is not offered Create Content, and the form explains why", async () => {
    const server = createServer("owner", { mutationsEnabled: false });
    renderApp(server, `/projects/${PROJECT_ID}/content`);

    await screen.findByRole("heading", { name: "Content" });
    expect(screen.queryByRole("link", { name: "Create Content" })).not.toBeInTheDocument();

    const direct = createServer("owner", { mutationsEnabled: false });
    renderApp(direct, `/projects/${PROJECT_ID}/content/new`);

    expect(await screen.findByText(/работает только на чтение/)).toBeInTheDocument();
  });

  it("reading still works: audit stays visible to an admin", async () => {
    const server = createServer("admin", { mutationsEnabled: false });
    renderApp(server, `/projects/${PROJECT_ID}`);

    expect(await screen.findByRole("link", { name: "Audit" })).toBeInTheDocument();
  });

  it("an /auth/me without control_plane is treated as read-only", async () => {
    const server = createServer("owner");
    server.on("GET", "/api/v1/auth/me", () => {
      const legacy = {
        user: {
          id: "u",
          email: "operator@example.com",
          display_name: "Operator",
          is_active: true,
          last_login_at: null,
          created_at: "2026-10-10T09:00:00+00:00",
        },
        projects: [{ project_id: PROJECT_ID, display_name: "Demo Project", role: "owner" }],
      };

      return ok(legacy);
    });
    renderApp(server, `/publications/${PUBLICATION_ID}`);

    await screen.findByRole("heading", { name: /Snapshot title/ });
    expect(screen.getByTestId("read-only-banner")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Schedule" })).not.toBeInTheDocument();
  });
});

describe("editable deployment (control)", () => {
  it("owner gets no read-only notice and is offered Schedule", async () => {
    const server = createServer("owner");
    renderApp(server, `/publications/${PUBLICATION_ID}`);

    expect(await screen.findByRole("button", { name: "Schedule" })).toBeInTheDocument();
    expect(screen.queryByTestId("read-only-banner")).not.toBeInTheDocument();
  });

  it("a MUTATIONS_DISABLED refusal is explained, not shown as a role problem", async () => {
    // The deployment was switched to read-only after this tab loaded.
    const server = createServer("admin");
    server.on(
      "POST",
      "/api/v1/publications/:id/cancel",
      apiError(403, "MUTATIONS_DISABLED", "This Control Center is read-only."),
    );
    const { user } = renderApp(server, `/publications/${PUBLICATION_ID}`);

    await user.click(await screen.findByRole("button", { name: "Cancel" }));
    await within(screen.getByRole("dialog")).findByText("Exact immutable snapshot body");
    await user.click(screen.getByRole("button", { name: "Cancel publication for good" }));

    await waitFor(() => expect(screen.getByText(/Только чтение/)).toBeInTheDocument());
    expect(screen.queryByText(/Недостаточно прав/)).not.toBeInTheDocument();
  });
});

describe("worker mode", () => {
  const dashboard = async (summary: Parameters<typeof makeSummary>[0]) => {
    const server = createServer("viewer", { mutationsEnabled: false });
    server.on("GET", "/api/v1/projects/:projectId/operations/summary", ok(makeSummary(summary)));
    renderApp(server, `/projects/${PROJECT_ID}`);

    return screen.findByTestId("worker-mode");
  };

  it("API dry_run=true is NOT presented as the worker being in dry run", async () => {
    const panel = await dashboard({ api_dry_run: true, worker_mode: "unknown" });

    expect(panel).toHaveTextContent("UNKNOWN");
    expect(panel).not.toHaveTextContent("DRY RUN");
    expect(panel).not.toHaveTextContent("ничего не отправляет");
    // The API's own flag is shown, labelled as the API's.
    expect(panel).toHaveTextContent("AI_SMM_DRY_RUN процесса API: true");
    expect(panel).toHaveTextContent("не к worker");
  });

  it("UNKNOWN warns that a scheduled publication may go out", async () => {
    const panel = await dashboard({ worker_mode: "unknown" });

    expect(panel).toHaveTextContent("может быть отправлена");
  });

  it("LIVE is shown only when the server reports it, with its source", async () => {
    const panel = await dashboard({ worker_mode: "live", worker_mode_source: "worker-heartbeat" });

    expect(panel).toHaveTextContent("LIVE");
    expect(panel).toHaveTextContent("worker-heartbeat");
  });

  it("DRY RUN is shown only when the server reports it", async () => {
    const panel = await dashboard({
      api_dry_run: false,
      worker_mode: "dry_run",
      worker_mode_source: "worker-heartbeat",
    });

    expect(panel).toHaveTextContent("DRY RUN");
  });
});
