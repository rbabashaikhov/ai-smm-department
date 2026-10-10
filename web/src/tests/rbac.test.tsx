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
  page,
} from "../test/fixtures";
import type { ContentStatus } from "../types/api";
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

describe("viewer is read-only", () => {
  it.each<ContentStatus>(["draft", "in_review", "approved"])(
    "content detail in %s offers no write action",
    async (status) => {
      const server = createServer("viewer", { item: makeItem(status) });
      renderApp(server, `/content/${ITEM_ID}`);

      await screen.findByRole("heading", { name: "Launch post" });

      for (const name of WRITE_BUTTONS) {
        expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
      }
    },
  );

  it("sees no Create Content, no queue commands and no Audit", async () => {
    const server = createServer("viewer");
    server.on(
      "GET",
      "/api/v1/projects/:projectId/publications",
      ok(page([makePublication("approved"), makePublication("scheduled", { id: 2, title: "Second" })])),
    );
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/content`);

    await screen.findByRole("link", { name: "Launch post" });
    expect(screen.queryByRole("link", { name: "Create Content" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Audit" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: "Publications" }));
    await screen.findByRole("link", { name: "Second" });
    for (const name of ["Schedule", "Reschedule", "Cancel"]) {
      expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
    }
  });

  it("is not shown the audit trail and the page does not request it", async () => {
    const server = createServer("viewer");
    renderApp(server, `/projects/${PROJECT_ID}/audit`);

    expect(await screen.findByText(/доступен ролям admin и owner/)).toBeInTheDocument();
    expect(server.callsTo("GET", /\/audit$/)).toHaveLength(0);
  });
});

describe("editor", () => {
  it("can approve content but is offered no schedule/reschedule/cancel", async () => {
    const server = createServer("editor", { item: makeItem("in_review") });
    server.on(
      "GET",
      "/api/v1/projects/:projectId/publications",
      ok(page([makePublication("approved"), makePublication("failed", { id: 2, title: "Failed" })])),
    );
    const { user } = renderApp(server, `/content/${ITEM_ID}`);

    expect(await screen.findByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New Revision" })).toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: "Publications" }));
    await screen.findByRole("link", { name: "Failed" });
    for (const name of ["Schedule", "Reschedule", "Cancel"]) {
      expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
    }
  });

  it("gets no schedule control on the publication page either", async () => {
    const server = createServer("editor");
    renderApp(server, `/publications/${PUBLICATION_ID}`);

    await screen.findByText("Exact immutable snapshot body");
    expect(screen.queryByRole("button", { name: "Schedule" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
  });
});

describe("admin", () => {
  it("is offered schedule and cancel for an approved snapshot, reschedule for a scheduled one", async () => {
    const server = createServer("admin");
    server.on(
      "GET",
      "/api/v1/projects/:projectId/publications",
      ok(page([makePublication("approved"), makePublication("scheduled", { id: 2, title: "Queued" })])),
    );
    renderApp(server, `/projects/${PROJECT_ID}/publications`);

    const approvedRow = (await screen.findByRole("link", { name: "Snapshot title" })).closest("tr")!;
    expect(within(approvedRow).getByRole("button", { name: "Schedule" })).toBeInTheDocument();
    expect(within(approvedRow).getByRole("button", { name: "Cancel" })).toBeInTheDocument();
    expect(within(approvedRow).queryByRole("button", { name: "Reschedule" })).not.toBeInTheDocument();

    const scheduledRow = screen.getByRole("link", { name: "Queued" }).closest("tr")!;
    expect(within(scheduledRow).getByRole("button", { name: "Reschedule" })).toBeInTheDocument();
    expect(within(scheduledRow).queryByRole("button", { name: "Schedule" })).not.toBeInTheDocument();
  });

  it("sees Audit in the sidebar", async () => {
    const server = createServer("admin");
    renderApp(server, `/projects/${PROJECT_ID}`);

    expect(await screen.findByRole("link", { name: "Audit" })).toBeInTheDocument();
  });
});

describe("the backend stays the authority", () => {
  it("a 403 from the server is shown even if the UI offered the action", async () => {
    const server = createServer("admin");
    server.on(
      "POST",
      "/api/v1/publications/:id/cancel",
      apiError(403, "FORBIDDEN", "Insufficient role for this action."),
    );
    const { user } = renderApp(server, `/publications/${PUBLICATION_ID}`);

    await user.click(await screen.findByRole("button", { name: "Cancel" }));
    await within(screen.getByRole("dialog")).findByText("Exact immutable snapshot body");
    await user.click(screen.getByRole("button", { name: "Cancel publication for good" }));

    await waitFor(() => expect(screen.getByText(/Недостаточно прав/)).toBeInTheDocument());
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });
});
