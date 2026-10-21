// SMM-034A: the project's Knowledge Base page, read-only.

import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { apiError, ok } from "../test/fakeApi";
import { PROJECT_ID, createServer } from "../test/fixtures";
import { currentPath, renderApp } from "../test/render";
import type { KnowledgeBase, Role } from "../types/api";

const T = "2026-10-21T09:00:00Z";

const KB: KnowledgeBase = {
  project_id: PROJECT_ID,
  available: true,
  writable: false,
  documents: [
    { name: "overview.md", kind: "markdown", size_bytes: 40, updated_at: T },
    { name: "project.yaml", kind: "yaml", size_bytes: 12, updated_at: T },
  ],
};

const OVERVIEW = "# Обзор\n<script>alert(1)</script>\nТекст для агентов.";

function withKnowledge(role: Role, kb: KnowledgeBase = KB) {
  return createServer(role)
    .on("GET", "/api/v1/projects/:projectId/knowledge", ok(kb))
    .on(
      "GET",
      "/api/v1/projects/:projectId/knowledge/:name",
      ({ params }) =>
        params.name === "overview.md"
          ? ok({ ...KB.documents[0], sha256: "a".repeat(64), content: OVERVIEW })
          : apiError(404, "NOT_FOUND", "Knowledge document not found."),
    );
}

describe("knowledge base", () => {
  it.each<Role>(["viewer", "owner"])(
    "%s opens the page from the sidebar, picks a document and reads it",
    async (role) => {
      const server = withKnowledge(role);
      const { user } = renderApp(server, `/projects/${PROJECT_ID}`);

      await user.click(await screen.findByRole("link", { name: "Knowledge Base" }));
      expect(currentPath()).toBe(`/projects/${PROJECT_ID}/knowledge`);

      const list = await screen.findByRole("list", { name: "Документы" });
      expect(within(list).getAllByRole("link").map((a) => a.textContent)).toEqual([
        "overview.md",
        "project.yaml",
      ]);
      expect(screen.getByTestId("knowledge-read-only")).toHaveTextContent("Только просмотр");

      await user.click(within(list).getByRole("link", { name: "overview.md" }));

      const content = await screen.findByLabelText("Содержимое документа");
      // Shown as text, never rendered as HTML.
      expect(content.textContent).toBe(OVERVIEW);
      expect(document.querySelector("script")).toBeNull();
      expect(currentPath()).toBe(`/projects/${PROJECT_ID}/knowledge?doc=overview.md`);
    },
  );

  it("offers no way to change a document, even to the owner", async () => {
    const server = withKnowledge("owner");
    renderApp(server, `/projects/${PROJECT_ID}/knowledge?doc=overview.md`);

    await screen.findByLabelText("Содержимое документа");

    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Сохранить|Save/ })).not.toBeInTheDocument();
    expect(server.calls.filter((c) => c.method !== "GET")).toEqual([]);
  });

  it("does not request a document the list does not name", async () => {
    const server = withKnowledge("owner");
    renderApp(server, `/projects/${PROJECT_ID}/knowledge?doc=..%2Fother%2Fsecret.md`);

    expect(await screen.findByText("Выберите документ слева.")).toBeInTheDocument();
    expect(server.callsTo("GET", /\/knowledge\/.+/)).toEqual([]);
  });

  it("says so when the deployment has no knowledge directory", async () => {
    const server = withKnowledge("owner", { ...KB, available: false, documents: [] });
    renderApp(server, `/projects/${PROJECT_ID}/knowledge`);

    expect(await screen.findByTestId("knowledge-unavailable")).toHaveTextContent(
      "недоступна",
    );
  });
});
