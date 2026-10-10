// The control center must not be able to publish. These tests check the
// rendered UI for every route and the source tree itself.

import { screen } from "@testing-library/react";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

import { ok } from "../test/fakeApi";
import {
  ITEM_ID,
  PROJECT_ID,
  PUBLICATION_ID,
  createServer,
  makeItem,
  makePublication,
  page,
} from "../test/fixtures";
import type { PublicationStatus } from "../types/api";
import { renderApp } from "../test/render";

const FORBIDDEN_CONTROL = /\bpublish\b|publish[\s_-]?now|retry[\s_-]?publish|опубликовать/i;

const ROUTES = [
  "/projects",
  `/projects/${PROJECT_ID}`,
  `/projects/${PROJECT_ID}/content`,
  `/projects/${PROJECT_ID}/content/new`,
  `/projects/${PROJECT_ID}/publications`,
  `/projects/${PROJECT_ID}/attention`,
  `/projects/${PROJECT_ID}/audit`,
  `/content/${ITEM_ID}`,
  `/publications/${PUBLICATION_ID}`,
];

const ALL_STATUSES: PublicationStatus[] = [
  "draft",
  "approved",
  "scheduled",
  "claimed",
  "publishing",
  "published",
  "failed",
  "needs_review",
  "cancelled",
];

describe("no publish control anywhere", () => {
  it.each(ROUTES)("owner on %s sees no Publish / Publish Now / Retry Publish", async (path) => {
    const server = createServer("owner", { item: makeItem("approved") });
    server
      .on(
        "GET",
        "/api/v1/projects/:projectId/publications",
        ok(page(ALL_STATUSES.map((status, i) => makePublication(status, { id: i + 1, title: `P ${status}` })))),
      )
      .on(
        "GET",
        "/api/v1/projects/:projectId/operations/attention",
        ok(page([makePublication("failed"), makePublication("needs_review", { id: 2 })])),
      );
    renderApp(server, path);

    await screen.findByRole("navigation");
    await new Promise((resolve) => setTimeout(resolve, 50));

    const controls = [...screen.queryAllByRole("button"), ...screen.queryAllByRole("link")];
    const offending = controls
      .map((el) => el.textContent?.trim() ?? "")
      .filter((text) => FORBIDDEN_CONTROL.test(text));

    expect(controls.length).toBeGreaterThan(0);
    expect(offending).toEqual([]);
  });
});

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);

    if (statSync(path).isDirectory()) return name === "test" || name === "tests" ? [] : sourceFiles(path);

    return /\.(ts|tsx)$/.test(name) && !/\.test\.tsx?$/.test(name) ? [path] : [];
  });
}

describe("source tree", () => {
  const root = join(process.cwd(), "src");
  const files = sourceFiles(root).map((path) => ({
    path: relative(root, path),
    text: readFileSync(path, "utf8"),
  }));

  it("has source files to check", () => {
    expect(files.length).toBeGreaterThan(20);
  });

  it("calls no publish endpoint and never talks to Threads", () => {
    for (const { path, text } of files) {
      expect(text, path).not.toMatch(/\/publish(?![a-z])/i);
      expect(text, path).not.toMatch(/publish[-_]?now/i);
      expect(text, path).not.toMatch(/threads\.net|graph\.threads/i);
      expect(text, path).not.toMatch(/ThreadsPublisher/);
    }
  });

  it("keeps fetch in the API client only", () => {
    const callers = files.filter(({ text }) => /\bfetch\(/.test(text)).map((f) => f.path);

    expect(callers).toEqual(["api/client.ts"]);
  });

  it("never stores anything in web storage", () => {
    for (const { path, text } of files) {
      expect(text, path).not.toMatch(/\b(localStorage|sessionStorage|indexedDB)\s*[.[]|document\.cookie/);
    }
  });

  it("never PATCHes or PUTs a revision", () => {
    const endpoints = files.find((f) => f.path === "api/endpoints.ts")!.text;

    expect(endpoints).not.toMatch(/"PATCH"|"PUT"|\.patch\(|\.put\(/);
  });
});
