import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ok } from "../test/fakeApi";
import { PROJECT_ID, createServer, makePublication, page } from "../test/fixtures";
import { currentPath, renderApp } from "../test/render";

describe("projects", () => {
  it("lists projects with id, role and settings, and opens one", async () => {
    const server = createServer("editor");
    const { user } = renderApp(server, "/projects");

    const row = (await screen.findByText("Demo Project", { selector: "strong" })).closest("tr")!;
    expect(within(row).getByText(PROJECT_ID)).toBeInTheDocument();
    expect(within(row).getByText("editor")).toBeInTheDocument();
    expect(within(row).getByText("Europe/Moscow")).toBeInTheDocument();
    expect(await within(row).findByText(/v2: content 1/)).toBeInTheDocument();

    await user.click(within(row).getByRole("link", { name: "Open" }));
    await waitFor(() => expect(currentPath()).toBe(`/projects/${PROJECT_ID}`));
  });
});

describe("dashboard", () => {
  it("shows backend projections and content totals per status", async () => {
    const server = createServer("viewer");
    server.on("GET", "/api/v1/projects/:projectId/content-items", (req) =>
      ok(page([], req.query.get("status") === "in_review" ? 7 : 0, 1)),
    );
    renderApp(server, `/projects/${PROJECT_ID}`);

    const metric = async (label: string) =>
      (await screen.findByText(label)).closest(".metric") as HTMLElement;

    expect(within(await metric("Publications total")).getByText("12")).toBeInTheDocument();
    expect(within(await metric("Due now")).getByText("1")).toBeInTheDocument();
    expect(within(await metric("Needs attention")).getByText("2")).toBeInTheDocument();
    expect(within(await metric("Published, last 24h")).getByText("4")).toBeInTheDocument();
    expect(within(await metric("Series active / total")).getByText("1 / 2")).toBeInTheDocument();
    expect(within(await metric("Next scheduled")).getByText(/12\.10\.2026, 09:00 \(UTC\+03:00\)/)).toBeInTheDocument();

    const inReview = await screen.findByRole("link", { name: "7" });
    expect(inReview).toHaveAttribute("href", `/projects/${PROJECT_ID}/content?status=in_review`);
    expect(server.callsTo("GET", "/api/v1/projects/demo/content-items")).toHaveLength(5);
  });
});

describe("attention", () => {
  it("lists failed and needs_review rows and offers no reconcile or retry", async () => {
    const failed = makePublication("failed", { id: 50, title: "Broken", attempt_count: 3, last_error: "rate limited" });
    const unknown = makePublication("needs_review", { id: 51, title: "Unknown outcome", attempt_count: 1 });
    const server = createServer("owner");
    server
      .on("GET", "/api/v1/projects/:projectId/operations/attention", ok(page([failed, unknown])))
      .on("GET", "/api/v1/publications/50", ok(failed))
      .on("GET", "/api/v1/publications/51", ok(unknown));
    renderApp(server, `/projects/${PROJECT_ID}/attention`);

    expect(await screen.findByRole("link", { name: "Broken" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Unknown outcome" })).toBeInTheDocument();
    expect(await screen.findByText("rate limited")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /reconcile|retry/i })).not.toBeInTheDocument();
    // needs_review is never offered a queue command; failed may be rescheduled by an admin.
    const unknownRow = screen.getByRole("link", { name: "Unknown outcome" }).closest("tr")!;
    expect(within(unknownRow).queryAllByRole("button")).toHaveLength(0);
  });
});

describe("audit", () => {
  it("admin sees who did what, with credential-like details masked", async () => {
    const server = createServer("admin");
    server.on(
      "GET",
      "/api/v1/projects/:projectId/audit",
      ok(
        page([
          {
            id: 1,
            at: "2026-10-10T09:00:00+00:00",
            actor: "user:abc",
            action: "content.approved",
            subject: "content_item:xyz",
            details: { revision_id: "r1", csrf_token: "should-not-show" },
          },
        ]),
      ),
    );
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/audit`);

    expect(await screen.findByText("content.approved")).toBeInTheDocument();
    expect(screen.getByText("user:abc")).toBeInTheDocument();
    expect(screen.getByText("content_item:xyz")).toBeInTheDocument();
    expect(screen.queryByText(/should-not-show/)).not.toBeInTheDocument();
    expect(screen.getByText(/\[redacted\]/)).toBeInTheDocument();

    await user.type(screen.getByLabelText("Action filter"), "cancelled");
    await user.click(screen.getByRole("button", { name: "Применить" }));
    await waitFor(() =>
      expect(server.callsTo("GET", "/api/v1/projects/demo/audit").at(-1)!.query.get("action")).toBe(
        "cancelled",
      ),
    );
  });
});
