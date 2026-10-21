import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ok, type FakeApi } from "../test/fakeApi";
import {
  ITEM_ID,
  REV2_ID,
  createServer,
  makeItem,
  makeRevision,
} from "../test/fixtures";
import { renderApp } from "../test/render";
import type { ContentStatus, JsonObject } from "../types/api";

const verdictBlock = () => screen.findByTestId("editor-verdict");

const aiRevision = (
  verdict: JsonObject | unknown,
  overrides: Parameters<typeof makeRevision>[0] = {},
) =>
  makeRevision({
    source: "copywriter",
    source_ref: "job:1",
    editor_score: 8.5,
    editor_notes: "Сильный хук, ровный ритм",
    metadata: { version: 1, editor_verdict: verdict } as JsonObject,
    ...overrides,
  });

const open = (server: FakeApi) => renderApp(server, `/content/${ITEM_ID}`);

const itemWith = (revision = aiRevision({}), status: ContentStatus = "in_review") =>
  makeItem(status, { current_revision: revision });

describe("editor verdict", () => {
  it("shows a passed verdict with scores, feedback and no issues", async () => {
    const server = createServer(
      "owner",
      {
        item: itemWith(
          aiRevision({
            approved: true,
            score: 8.5,
            narrative_score: 7.5,
            issues: [],
            revision_count: 2,
          }),
        ),
      },
    );
    open(server);

    const block = await verdictBlock();
    expect(within(block).getByTestId("editor-verdict-result")).toHaveTextContent(/^Passed/);
    expect(within(block).getByText("Editor score: 8.5")).toBeInTheDocument();
    expect(within(block).getByText("Narrative score: 7.5")).toBeInTheDocument();
    expect(within(block).getByText(/Revision count.*: 2/)).toBeInTheDocument();
    expect(within(block).getByText("Сильный хук, ровный ритм")).toBeInTheDocument();
    expect(within(block).queryByText("Issues:")).not.toBeInTheDocument();
    // An AI assessment, explicitly not a human approval.
    expect(within(block).getByText(/не одобрение человеком/)).toBeInTheDocument();
  });

  it("shows a failed verdict with every issue", async () => {
    const server = createServer(
      "owner",
      {
        item: itemWith(
          aiRevision({
            approved: false,
            score: 4,
            narrative_score: 3,
            issues: ["Слишком длинно", "Нет призыва к действию", "Повтор тезиса"],
            revision_count: 3,
          }),
        ),
      },
    );
    open(server);

    const block = await verdictBlock();
    expect(within(block).getByTestId("editor-verdict-result")).toHaveTextContent(/^Needs improvement/);
    expect(within(block).getByText("Issues:")).toBeInTheDocument();
    for (const issue of ["Слишком длинно", "Нет призыва к действию", "Повтор тезиса"]) {
      expect(within(block).getByText(issue)).toBeInTheDocument();
    }
  });

  it("tolerates missing, null and malformed fields without inventing values", async () => {
    const server = createServer(
      "owner",
      {
        item: itemWith(
          aiRevision(
            {
              approved: "yes",
              score: "high",
              narrative_score: null,
              issues: "oops",
              revision_count: -1,
            },
            { editor_score: null, editor_notes: "" },
          ),
        ),
      },
    );
    open(server);

    const block = await verdictBlock();
    expect(within(block).getByTestId("editor-verdict-result")).toHaveTextContent(
      "Результат AI Editor не указан",
    );
    expect(within(block).queryByText(/score/i)).not.toBeInTheDocument();
    expect(within(block).queryByText(/Revision count/)).not.toBeInTheDocument();
    expect(within(block).queryByText("Issues:")).not.toBeInTheDocument();
    expect(within(block).queryByText("Passed")).not.toBeInTheDocument();
  });

  it("says so when the stored verdict is not an object, and shows nothing for non-object metadata", async () => {
    const server = createServer("owner", { item: itemWith(aiRevision("garbage")) });
    open(server);

    expect(await verdictBlock()).toHaveTextContent(/неизвестном формате/);
  });

  it("shows no AI status for a manual revision", async () => {
    // Manual revision carrying an editor_score/notes of its own: still no verdict.
    const server = createServer("owner", { item: makeItem("draft") });
    open(server);

    await screen.findByRole("heading", { name: "Launch post" });
    expect(screen.queryByTestId("editor-verdict")).not.toBeInTheDocument();
    expect(screen.queryByText(/AI Editor/)).not.toBeInTheDocument();
  });

  it("does not show the old AI verdict after a manual New Revision", async () => {
    let item = itemWith(
      aiRevision({ approved: true, score: 9, issues: ["Старое замечание"], revision_count: 1 }),
    );
    const server = createServer("owner", { item });
    server.on("GET", "/api/v1/content-items/:id", () => ok(item));
    server.on("POST", "/api/v1/content-items/:id/revisions", (request) => {
      // The server stores exactly the metadata the form sent, with source human.
      const sent = request.body as { metadata: JsonObject; source: string; body: string };
      const manual = makeRevision({
        id: REV2_ID,
        revision_number: 2,
        body: sent.body,
        source: sent.source as "human",
        metadata: sent.metadata,
      });
      item = makeItem("draft", { current_revision: manual, version: 4 });

      return ok({ item, revision: manual }, 201);
    });
    const { user } = open(server);

    expect(await screen.findByText("Старое замечание")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "New Revision" }));
    await user.type(screen.getByLabelText("Body"), " + правка");
    await user.click(screen.getByRole("button", { name: "Save as new revision" }));

    await waitFor(() => expect(screen.queryByTestId("editor-verdict")).not.toBeInTheDocument());
    expect(screen.queryByText("Старое замечание")).not.toBeInTheDocument();

    const [post] = server.callsTo("POST", `/api/v1/content-items/${ITEM_ID}/revisions`);
    expect(post!.body).toMatchObject({ source: "human" });
    // Stale verdict is not sent; unrelated metadata is preserved.
    const metadata = (post!.body as { metadata: JsonObject }).metadata;
    expect(metadata).not.toHaveProperty("editor_verdict");
    expect(metadata).toHaveProperty("version", 1);
  });

  it("never shows an AI verdict for a human revision, even with inherited metadata", async () => {
    const server = createServer("owner", {
      item: itemWith(aiRevision({ approved: true, score: 9, issues: [] }, { source: "human" })),
    });
    open(server);

    await screen.findByRole("heading", { name: "Launch post" });
    expect(screen.queryByTestId("editor-verdict")).not.toBeInTheDocument();
    expect(screen.queryByText(/Passed/)).not.toBeInTheDocument();
  });

  it("never changes the human approval state or calls a mutation endpoint", async () => {
    const server = createServer(
      "owner",
      {
        item: itemWith(aiRevision({ approved: true, score: 9, issues: [], revision_count: 0 })),
      },
    );
    open(server);

    expect(await verdictBlock()).toBeInTheDocument();
    // AI "approved" is not a human approval: the item still awaits a human.
    expect(screen.getByText(/ждёт решения человека/)).toBeInTheDocument();
    expect(screen.queryByText(/одобрение действует/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(server.calls.filter((c) => !["GET", "HEAD"].includes(c.method))).toEqual([]);
  });

  it("is readable by a viewer, who has no edit or approval actions", async () => {
    const server = createServer(
      "viewer",
      { item: itemWith(aiRevision({ approved: false, score: 5, issues: ["Вода"], revision_count: 1 })) },
    );
    open(server);

    const block = await verdictBlock();
    expect(within(block).getByText("Вода")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New Revision" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });
});
