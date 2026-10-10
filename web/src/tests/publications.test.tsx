import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { apiError, ok } from "../test/fakeApi";
import {
  PROJECT_ID,
  PUBLICATION_ID,
  createServer,
  makePublication,
  page,
} from "../test/fixtures";
import { renderApp } from "../test/render";

const dialog = () => screen.getByRole("dialog");

describe("publication list", () => {
  it("shows the delivery columns and passes filters to the API", async () => {
    const failed = makePublication("failed", {
      id: 43,
      title: "Failed one",
      attempt_count: 3,
      series_id: 5,
      series_position: 2,
      series_total: 4,
      last_error: "Threads said 500",
    });
    const server = createServer("viewer");
    server
      .on("GET", "/api/v1/projects/:projectId/publications", ok(page([makePublication(), failed])))
      .on("GET", "/api/v1/publications/43", ok(failed));
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/publications`);

    const row = (await screen.findByRole("link", { name: "Failed one" })).closest("tr")!;
    expect(within(row).getByText("failed")).toBeInTheDocument();
    expect(within(row).getByText("#5 · 2/4")).toBeInTheDocument();
    expect(within(row).getByText("3")).toBeInTheDocument();
    expect(await within(row).findByText("Threads said 500")).toBeInTheDocument();
    expect(within(row).getByRole("link", { name: "Preview" })).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Status filter"), "scheduled");
    await user.selectOptions(screen.getByLabelText("Reviewed filter"), "true");
    await waitFor(() => {
      const last = server.callsTo("GET", "/api/v1/projects/demo/publications").at(-1)!;
      expect(last.query.getAll("status")).toEqual(["scheduled"]);
      expect(last.query.get("human_reviewed")).toBe("true");
    });
  });
});

describe("publication detail", () => {
  it("shows the immutable snapshot, preflight, attempts and claim, with nothing editable", async () => {
    const detail = makePublication("scheduled", {
      claimed_by: "worker-1",
      last_error: "transient",
      attempt_count: 1,
    });
    const server = createServer("viewer", { publication: detail });
    server.on(
      "GET",
      "/api/v1/publications/:id/attempts",
      ok(
        page([
          {
            id: 1,
            publication_id: PUBLICATION_ID,
            attempt_number: 1,
            worker_id: "worker-1",
            phase: "publish",
            outcome: "error",
            started_at: "2026-10-10T09:00:00+00:00",
            finished_at: "2026-10-10T09:00:02+00:00",
            threads_creation_id: null,
            threads_post_id: null,
            http_status: 500,
            error_type: "ThreadsServerError",
            error_message: "boom",
            details: {},
          },
        ]),
      ),
    );
    renderApp(server, `/publications/${PUBLICATION_ID}`);

    expect(await screen.findByText("Exact immutable snapshot body")).toBeInTheDocument();
    expect(screen.getByText(/Неизменяемый delivery snapshot/)).toBeInTheDocument();
    expect(await screen.findByText("ThreadsServerError")).toBeInTheDocument();
    expect(screen.getAllByText("worker-1").length).toBeGreaterThan(0);
    expect(screen.getByText("eligible for worker when due")).toBeInTheDocument();
    expect(screen.getByText("transient")).toBeInTheDocument();
    expect(screen.queryAllByRole("textbox")).toHaveLength(0);
  });
});

describe("scheduling", () => {
  it("admin: the confirmation shows the exact snapshot and sends an offset-aware time", async () => {
    const server = createServer("admin");
    server.on("POST", "/api/v1/publications/:id/schedule", ok(makePublication("scheduled")));
    const { user } = renderApp(server, `/projects/${PROJECT_ID}/publications`);

    const row = (await screen.findByRole("link", { name: "Snapshot title" })).closest("tr")!;
    const detailReads = server.callsTo("GET", `/api/v1/publications/${PUBLICATION_ID}`).length;
    await user.click(within(row).getByRole("button", { name: "Schedule" }));

    const modal = dialog();
    expect(await within(modal).findByText("Exact immutable snapshot body")).toBeInTheDocument();
    expect(within(modal).getByText("Snapshot title")).toBeInTheDocument();
    // The dialog re-read the row rather than trusting the list.
    expect(server.callsTo("GET", `/api/v1/publications/${PUBLICATION_ID}`).length).toBeGreaterThan(
      detailReads,
    );
    expect(within(modal).getAllByText(/Europe\/Moscow/).length).toBeGreaterThan(0);

    fireEvent.change(await within(modal).findByLabelText("Scheduled time"), {
      target: { value: "2026-10-12T09:00" },
    });
    expect(within(modal).getByText("2026-10-12T09:00:00+03:00")).toBeInTheDocument();
    expect(within(modal).getByText("2026-10-12T06:00:00Z")).toBeInTheDocument();
    expect(server.callsTo("POST", /schedule$/)).toHaveLength(0);

    await user.click(within(modal).getByRole("button", { name: "Confirm schedule" }));

    await waitFor(() => expect(server.callsTo("POST", /\/schedule$/)).toHaveLength(1));
    const [call] = server.callsTo("POST", /\/schedule$/);
    expect(call!.body).toEqual({ scheduled_at: "2026-10-12T09:00:00+03:00" });
    expect(call!.headers["x-csrf-token"]).toBe(server.csrfToken);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("admin: reschedule starts from the current time in the project zone", async () => {
    const server = createServer("admin", { publication: makePublication("scheduled") });
    server.on("POST", "/api/v1/publications/:id/reschedule", ok(makePublication("scheduled")));
    const { user } = renderApp(server, `/publications/${PUBLICATION_ID}`);

    await user.click(await screen.findByRole("button", { name: "Reschedule" }));
    const input = await within(dialog()).findByLabelText("Scheduled time");
    expect(input).toHaveValue("2026-10-12T09:00");

    fireEvent.change(input, { target: { value: "2026-10-13T10:30" } });
    await user.click(within(dialog()).getByRole("button", { name: "Confirm reschedule" }));

    await waitFor(() => expect(server.callsTo("POST", /reschedule$/)).toHaveLength(1));
    expect(server.callsTo("POST", /reschedule$/)[0]!.body).toEqual({
      scheduled_at: "2026-10-13T10:30:00+03:00",
    });
  });

  it("admin: cancel needs an explicit confirmation", async () => {
    const server = createServer("admin");
    server.on("POST", "/api/v1/publications/:id/cancel", ok(makePublication("cancelled")));
    const { user } = renderApp(server, `/publications/${PUBLICATION_ID}`);

    await user.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(server.callsTo("POST", /cancel$/)).toHaveLength(0);
    expect(await within(dialog()).findByText("Exact immutable snapshot body")).toBeInTheDocument();

    await user.click(within(dialog()).getByRole("button", { name: "Cancel publication for good" }));
    await waitFor(() => expect(server.callsTo("POST", /cancel$/)).toHaveLength(1));
  });

  it("503 on a command shows a busy state, does not retry, and waits for Retry-After", async () => {
    const server = createServer("owner");
    server.on(
      "POST",
      "/api/v1/publications/:id/schedule",
      apiError(503, "DEPENDENCY_UNAVAILABLE", "This publication is being worked on right now; retry in a moment.", { reason: "publication_locked" }, { "Retry-After": "2" }),
    );
    const { user } = renderApp(server, `/publications/${PUBLICATION_ID}`);

    await user.click(await screen.findByRole("button", { name: "Schedule" }));
    fireEvent.change(await within(dialog()).findByLabelText("Scheduled time"), {
      target: { value: "2026-10-12T09:00" },
    });
    await user.click(within(dialog()).getByRole("button", { name: "Confirm schedule" }));

    expect(await within(dialog()).findByText(/Сервис занят/)).toBeInTheDocument();
    expect(within(dialog()).getByText(/Nothing was changed/)).toBeInTheDocument();
    expect(within(dialog()).getByText(/Можно повторить через 2 с/)).toBeInTheDocument();
    expect(within(dialog()).getByRole("button", { name: "Confirm schedule" })).toBeDisabled();

    await new Promise((resolve) => setTimeout(resolve, 2300));
    expect(server.callsTo("POST", /\/schedule$/)).toHaveLength(1);
    await waitFor(() =>
      expect(within(dialog()).getByRole("button", { name: "Confirm schedule" })).toBeEnabled(),
    );
  }, 10_000);

  it("shows the server's 409 INVALID_STATE_TRANSITION verbatim", async () => {
    const server = createServer("admin");
    server.on(
      "POST",
      "/api/v1/publications/:id/schedule",
      apiError(409, "INVALID_STATE_TRANSITION", "Cannot schedule a claimed publication.", {
        command: "schedule",
        status: "claimed",
        allowed_from: ["approved", "failed"],
      }),
    );
    const { user } = renderApp(server, `/publications/${PUBLICATION_ID}`);

    await user.click(await screen.findByRole("button", { name: "Schedule" }));
    await within(dialog()).findByLabelText("Scheduled time");
    await user.click(within(dialog()).getByRole("button", { name: "Confirm schedule" }));

    expect(await within(dialog()).findByText(/Cannot schedule a claimed publication/)).toBeInTheDocument();
    expect(within(dialog()).getByText(/Текущий статус: claimed/)).toBeInTheDocument();
  });
});

describe("503 on reads", () => {
  it("retries a read after Retry-After and then renders", async () => {
    const server = createServer("viewer");
    let calls = 0;
    server.on("GET", "/api/v1/publications/:id", () => {
      calls += 1;

      return calls === 1
        ? apiError(503, "DEPENDENCY_UNAVAILABLE", "busy", {}, { "Retry-After": "1" })
        : ok(makePublication());
    });
    renderApp(server, `/publications/${PUBLICATION_ID}`);

    expect(
      await screen.findByText("Exact immutable snapshot body", {}, { timeout: 4000 }),
    ).toBeInTheDocument();
    expect(calls).toBe(2);
  }, 10_000);
});
