import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { apiError } from "../test/fakeApi";
import { createServer } from "../test/fixtures";
import { currentPath, renderApp } from "../test/render";

describe("auth bootstrap", () => {
  it("loads me, then the CSRF token, then the protected page", async () => {
    const server = createServer("owner");
    renderApp(server, "/projects");

    expect(await screen.findByRole("heading", { name: "Проекты" })).toBeInTheDocument();

    const order = server.calls.map((c) => `${c.method} ${c.path}`);
    const me = order.indexOf("GET /api/v1/auth/me");
    const csrf = order.indexOf("GET /api/v1/auth/csrf");
    const projects = order.indexOf("GET /api/v1/projects");

    expect(me).toBe(0);
    expect(csrf).toBeGreaterThan(me);
    expect(projects).toBeGreaterThan(csrf);
  });

  it("redirects an unauthenticated visitor to /login and asks for no CSRF token", async () => {
    const server = createServer(null);
    renderApp(server, "/projects/demo/content");

    expect(await screen.findByRole("form", { name: "Login" })).toBeInTheDocument();
    expect(currentPath()).toBe("/login");
    expect(server.callsTo("GET", "/api/v1/auth/csrf")).toHaveLength(0);
    expect(server.callsTo("GET", /content-items/)).toHaveLength(0);
  });

  it("sends a user whose session expires mid-way back to /login", async () => {
    const server = createServer("owner");
    renderApp(server, "/projects");
    await screen.findByRole("heading", { name: "Проекты" });

    server.on("GET", "/api/v1/projects/:projectId/operations/summary", apiError(401, "AUTH_REQUIRED"));
    server.on("GET", "/api/v1/projects/:projectId", apiError(401, "AUTH_REQUIRED"));
    await screen.findByRole("link", { name: "Open" }).then((link) => link.click());

    await waitFor(() => expect(currentPath()).toBe("/login"));
  });
});

describe("login", () => {
  it("logs in, then me -> csrf -> projects, and redirects into the app", async () => {
    const server = createServer(null);
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const { user } = renderApp(server, "/login");

    await user.type(await screen.findByLabelText("Email"), "operator@example.com");
    await user.type(screen.getByLabelText("Password"), "correct horse");
    await user.click(screen.getByRole("button", { name: "Войти" }));

    expect(await screen.findByRole("heading", { name: "Проекты" })).toBeInTheDocument();
    expect(currentPath()).toBe("/projects");

    const login = server.callsTo("POST", "/api/v1/auth/login")[0]!;
    expect(login.body).toEqual({ email: "operator@example.com", password: "correct horse" });
    expect(login.headers["x-csrf-token"]).toBeUndefined();

    const order = server.calls.map((c) => `${c.method} ${c.path}`);
    const after = order.slice(order.indexOf("POST /api/v1/auth/login"));
    expect(after.slice(0, 4)).toEqual([
      "POST /api/v1/auth/login",
      "GET /api/v1/auth/me",
      "GET /api/v1/auth/csrf",
      "GET /api/v1/projects",
    ]);

    // Nothing about the session reaches web storage.
    expect(setItem).not.toHaveBeenCalled();
  });

  it("shows one generic message for bad credentials", async () => {
    const server = createServer(null);
    server.on("POST", "/api/v1/auth/login", apiError(401, "INVALID_CREDENTIALS", "Invalid email or password."));
    const { user } = renderApp(server, "/login");

    await user.type(await screen.findByLabelText("Email"), "a@b.c");
    await user.type(screen.getByLabelText("Password"), "wrong");
    await user.click(screen.getByRole("button", { name: "Войти" }));

    expect(await screen.findByText("Неверный email или пароль.")).toBeInTheDocument();
    expect(currentPath()).toBe("/login");
    expect(screen.getByLabelText("Password")).toHaveValue("");
  });
});

describe("logout", () => {
  it("POSTs logout with the CSRF token and returns to /login", async () => {
    const server = createServer("editor");
    const { user } = renderApp(server, "/projects");
    await screen.findByRole("heading", { name: "Проекты" });

    await user.click(screen.getByRole("button", { name: "Выйти" }));

    await waitFor(() => expect(currentPath()).toBe("/login"));
    const logout = server.callsTo("POST", "/api/v1/auth/logout");
    expect(logout).toHaveLength(1);
    expect(logout[0]!.headers["x-csrf-token"]).toBe(server.csrfToken);
  });

  it("does not send the next login back to the previous session's page", async () => {
    const server = createServer("editor");
    const { user } = renderApp(server, "/projects/demo/content");
    await screen.findByRole("link", { name: "Launch post" });

    await user.click(screen.getByRole("button", { name: "Выйти" }));
    await waitFor(() => expect(currentPath()).toBe("/login"));

    await user.type(await screen.findByLabelText("Email"), "someone@example.com");
    await user.type(screen.getByLabelText("Password"), "pw");
    await user.click(screen.getByRole("button", { name: "Войти" }));

    await waitFor(() => expect(currentPath()).toBe("/projects"));
  });
});
