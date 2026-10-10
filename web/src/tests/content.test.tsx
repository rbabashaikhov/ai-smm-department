import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { apiError, ok } from "../test/fakeApi";
import {
  ITEM_ID,
  PROJECT_ID,
  REV1_ID,
  REV2_ID,
  createServer,
  makeApproval,
  makeItem,
  makePublication,
  makeRevision,
  page,
} from "../test/fixtures";
import { currentPath, renderApp } from "../test/render";

const dialog = () => screen.getByRole("dialog");

describe("content list", () => {
  it("shows the columns and passes filters and pagination to the API", async () => {
    const server = createServer("editor");
    server.on(
      "GET",
      "/api/v1/projects/:projectId/content-items",
      ok(page([makeItem("approved")], 60, 25, 0)),
    );
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/content`);

    const row = (await screen.findByRole("link", { name: "Launch post" })).closest("tr")!;
    expect(within(row).getByText("approved")).toBeInTheDocument();
    expect(within(row).getByText("post")).toBeInTheDocument();
    expect(within(row).getAllByText(REV1_ID.slice(0, 8))).toHaveLength(2);
    expect(within(row).getByText("3")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Create Content" })).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Status filter"), "in_review");
    await waitFor(() => {
      const last = server.callsTo("GET", "/api/v1/projects/demo/content-items").at(-1)!;
      expect(last.query.getAll("status")).toEqual(["in_review"]);
    });

    await user.type(screen.getByLabelText("Content type filter"), "thread");
    await user.click(screen.getByRole("button", { name: "Применить" }));
    await waitFor(() => {
      const last = server.callsTo("GET", "/api/v1/projects/demo/content-items").at(-1)!;
      expect(last.query.get("content_type")).toBe("thread");
    });

    await user.click(screen.getByRole("button", { name: "Вперёд →" }));
    await waitFor(() => {
      const last = server.callsTo("GET", "/api/v1/projects/demo/content-items").at(-1)!;
      expect(last.query.get("offset")).toBe("25");
      expect(last.query.get("limit")).toBe("25");
    });
  });
});

describe("create content", () => {
  it("creates the item and its first revision in one request", async () => {
    const server = createServer("editor");
    server.on("POST", "/api/v1/projects/:projectId/content-items", ok(makeItem("draft"), 201));
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/content/new`);

    await user.type(await screen.findByLabelText("Title"), "Launch post");
    await user.type(screen.getByLabelText("Body"), "Hello world");
    await user.click(screen.getByRole("button", { name: "Create Content" }));

    await waitFor(() => expect(currentPath()).toBe(`/content/${ITEM_ID}`));

    const [create] = server.callsTo("POST", "/api/v1/projects/demo/content-items");
    expect(create!.headers["x-csrf-token"]).toBe(server.csrfToken);
    expect(create!.body).toMatchObject({
      title: "Launch post",
      content_type: "post",
      revision: { body: "Hello world", format: "text", source: "human" },
    });
    expect(create!.body).not.toHaveProperty("project_id");
  });
});

describe("content detail", () => {
  it("shows the current revision, metadata and both histories", async () => {
    const server = createServer("viewer", { item: makeItem("approved") });
    server.on(
      "GET",
      "/api/v1/content-items/:id/approvals",
      ok(page([makeApproval({ note: "Looks right" })])),
    );
    renderApp(server, `/content/${ITEM_ID}`);

    expect(await screen.findByRole("heading", { name: "Launch post" })).toBeInTheDocument();
    expect(screen.getAllByText("Exact body of revision one").length).toBeGreaterThan(0);
    expect(screen.getAllByText("brief:7").length).toBeGreaterThan(0);
    expect(screen.getByText("Tight copy")).toBeInTheDocument();
    expect(await screen.findByText("Looks right")).toBeInTheDocument();
    expect(screen.getByText("in effect")).toBeInTheDocument();
    expect(screen.getByText(/одобрение действует/)).toBeInTheDocument();
  });

  it("marks an approval of an older revision as superseded, not active", async () => {
    const item = makeItem("draft", {
      current_revision: makeRevision({ id: REV2_ID, revision_number: 2, body: "Second text" }),
      current_revision_id: REV2_ID,
    });
    const server = createServer("editor", { item });
    server.on("GET", "/api/v1/content-items/:id/approvals", ok(page([makeApproval()])));
    renderApp(server, `/content/${ITEM_ID}`);

    expect(await screen.findByText("superseded")).toBeInTheDocument();
    expect(screen.queryByText("in effect")).not.toBeInTheDocument();
    expect(screen.queryByText(/одобрение действует/)).not.toBeInTheDocument();
  });
});

describe("new revision", () => {
  it("POSTs a new revision with expected_item_version, never PATCH", async () => {
    const server = createServer("editor", { item: makeItem("approved") });
    server.on(
      "POST",
      "/api/v1/content-items/:id/revisions",
      ok({ item: makeItem("draft"), revision: makeRevision({ id: REV2_ID }) }, 201),
    );
    const { user } = renderApp(server, `/content/${ITEM_ID}`);

    await user.click(await screen.findByRole("button", { name: "New Revision" }));
    const body = screen.getByLabelText("Body");
    await user.clear(body);
    await user.type(body, "Edited text");
    await user.click(screen.getByRole("button", { name: "Save as new revision" }));

    await waitFor(() =>
      expect(server.callsTo("POST", `/api/v1/content-items/${ITEM_ID}/revisions`)).toHaveLength(1),
    );
    const [call] = server.callsTo("POST", `/api/v1/content-items/${ITEM_ID}/revisions`);
    expect(call!.body).toMatchObject({ expected_item_version: 3, body: "Edited text" });
    expect(server.calls.some((c) => c.method === "PATCH" || c.method === "PUT")).toBe(false);
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Save as new revision" })).not.toBeInTheDocument(),
    );
  });

  it("explains VERSION_CONFLICT and requires a reload instead of overwriting", async () => {
    const server = createServer("editor");
    server.on(
      "POST",
      "/api/v1/content-items/:id/revisions",
      apiError(409, "VERSION_CONFLICT", "stale", { expected_version: 3, current_version: 4 }),
    );
    const { user } = renderApp(server, `/content/${ITEM_ID}`);

    await user.click(await screen.findByRole("button", { name: "New Revision" }));
    await user.type(screen.getByLabelText("Body"), " more");
    await user.click(screen.getByRole("button", { name: "Save as new revision" }));

    const alert = await screen.findByText(/Контент был изменён другим действием/);
    expect(alert).toBeInTheDocument();
    expect(screen.getByText(/reload\s+required/)).toBeInTheDocument();
    // Exactly one attempt: no silent retry with a fresher version.
    expect(server.callsTo("POST", `/api/v1/content-items/${ITEM_ID}/revisions`)).toHaveLength(1);
    // The unsaved text is still available to copy.
    expect(screen.getByText("Ваш несохранённый текст")).toBeInTheDocument();

    const before = server.callsTo("GET", `/api/v1/content-items/${ITEM_ID}`).length;
    await user.click(screen.getByRole("button", { name: "Reload" }));
    await waitFor(() =>
      expect(server.callsTo("GET", `/api/v1/content-items/${ITEM_ID}`).length).toBeGreaterThan(before),
    );
  });
});

describe("review decisions", () => {
  it("submits the exact revision on screen for review", async () => {
    const server = createServer("editor", { item: makeItem("draft") });
    server.on("POST", "/api/v1/content-items/:id/submit-review", ok(makeItem("in_review")));
    const { user } = renderApp(server, `/content/${ITEM_ID}`);

    await user.click(await screen.findByRole("button", { name: "Submit Review" }));
    await user.click(within(dialog()).getByRole("button", { name: "Submit Review" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const [call] = server.callsTo("POST", `/api/v1/content-items/${ITEM_ID}/submit-review`);
    expect(call!.body).toEqual({ revision_id: REV1_ID, expected_item_version: 3 });
  });

  it("approves only after a confirmation that shows the exact revision", async () => {
    const server = createServer("editor", { item: makeItem("in_review") });
    server.on(
      "POST",
      "/api/v1/content-items/:id/approve",
      ok({ item: makeItem("approved"), approval: makeApproval() }),
    );
    const { user } = renderApp(server, `/content/${ITEM_ID}`);

    await user.click(await screen.findByRole("button", { name: "Approve" }));
    expect(server.callsTo("POST", /approve$/)).toHaveLength(0);

    const modal = dialog();
    expect(within(modal).getByText("Exact body of revision one")).toBeInTheDocument();
    expect(within(modal).getByText(REV1_ID)).toBeInTheDocument();
    expect(within(modal).getByText("#1")).toBeInTheDocument();

    await user.click(within(modal).getByRole("button", { name: "Approve this revision" }));

    await waitFor(() => expect(server.callsTo("POST", /approve$/)).toHaveLength(1));
    expect(server.callsTo("POST", /approve$/)[0]!.body).toEqual({
      revision_id: REV1_ID,
      expected_item_version: 3,
    });
  });

  it("rejects with an optional note", async () => {
    const server = createServer("editor", { item: makeItem("in_review") });
    server.on(
      "POST",
      "/api/v1/content-items/:id/reject",
      ok({ item: makeItem("rejected"), approval: makeApproval({ decision: "rejected" }) }),
    );
    const { user } = renderApp(server, `/content/${ITEM_ID}`);

    await user.click(await screen.findByRole("button", { name: "Reject" }));
    await user.type(within(dialog()).getByLabelText("Decision note"), "Wrong tone");
    await user.click(within(dialog()).getByRole("button", { name: "Reject this revision" }));

    await waitFor(() => expect(server.callsTo("POST", /reject$/)).toHaveLength(1));
    expect(server.callsTo("POST", /reject$/)[0]!.body).toEqual({
      revision_id: REV1_ID,
      expected_item_version: 3,
      note: "Wrong tone",
    });
  });

  it("on STALE_REVISION records nothing and reloads the current revision", async () => {
    const server = createServer("editor", { item: makeItem("in_review") });
    server.on(
      "POST",
      "/api/v1/content-items/:id/approve",
      apiError(409, "STALE_REVISION", "stale", {
        requested_revision_id: REV1_ID,
        current_revision_id: REV2_ID,
      }),
    );
    const { user } = renderApp(server, `/content/${ITEM_ID}`);
    await user.click(await screen.findByRole("button", { name: "Approve" }));

    // Someone else's revision 2 is what the server now has.
    server.on(
      "GET",
      "/api/v1/content-items/:id",
      ok(
        makeItem("draft", {
          current_revision: makeRevision({ id: REV2_ID, revision_number: 2, body: "Newer text" }),
          current_revision_id: REV2_ID,
          version: 4,
        }),
      ),
    );
    await user.click(within(dialog()).getByRole("button", { name: "Approve this revision" }));

    expect(await screen.findByText(/STALE_REVISION/)).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect((await screen.findAllByText("Newer text")).length).toBeGreaterThan(0);
  });
});

describe("materialize", () => {
  it("is called a delivery snapshot, confirms revision/hash/platform and posts the revision", async () => {
    const server = createServer("editor", { item: makeItem("approved") });
    server.on(
      "POST",
      "/api/v1/content-items/:id/materialize",
      ok({ result: "created", platform: "threads", revision_id: REV1_ID, publication: makePublication() }, 201),
    );
    const { user } = renderApp(server, `/content/${ITEM_ID}`);

    await user.click(await screen.findByRole("button", { name: "Подготовить публикацию" }));
    const modal = dialog();
    expect(within(modal).getByText(REV1_ID)).toBeInTheDocument();
    expect(within(modal).getByText(/^abcdef0123456789/)).toBeInTheDocument();
    expect(within(modal).getByText("threads")).toBeInTheDocument();
    expect(within(modal).queryByRole("button", { name: /^publish/i })).not.toBeInTheDocument();

    await user.click(within(modal).getByRole("button", { name: "Create delivery snapshot" }));

    await waitFor(() => expect(server.callsTo("POST", /materialize$/)).toHaveLength(1));
    expect(server.callsTo("POST", /materialize$/)[0]!.body).toEqual({ revision_id: REV1_ID });
    expect((await screen.findAllByRole("link", { name: "Publication #42" })).length).toBeGreaterThan(0);
  });

  it("on PUBLICATION_NOT_EDITABLE shows the blocking snapshot and never auto-cancels", async () => {
    const server = createServer("owner", { item: makeItem("approved") });
    server.on(
      "POST",
      "/api/v1/content-items/:id/materialize",
      apiError(409, "PUBLICATION_NOT_EDITABLE", "Previous delivery snapshot must be cancelled before a newer revision can be materialized.", {
        command: "materialize",
        publication_id: 17,
        publication_status: "scheduled",
        previous_revision_id: REV2_ID,
      }),
    );
    const { user } = renderApp(server, `/content/${ITEM_ID}`);

    await user.click(await screen.findByRole("button", { name: "Подготовить публикацию" }));
    await user.click(within(dialog()).getByRole("button", { name: "Create delivery snapshot" }));

    expect(await screen.findByText(/PUBLICATION_NOT_EDITABLE/)).toBeInTheDocument();
    expect(screen.getByText("#17")).toBeInTheDocument();
    expect(screen.getByText(REV2_ID)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open Publication #17" })).toHaveAttribute(
      "href",
      "/publications/17",
    );
    expect(server.callsTo("POST", /cancel$/)).toHaveLength(0);
    expect(server.callsTo("POST", /materialize$/)).toHaveLength(1);
  });
});
