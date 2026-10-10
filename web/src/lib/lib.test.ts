import { describe, expect, it } from "vitest";

import { REDACTED, redactSecrets } from "./redact";
import { toWallTime, zonedWallTimeToInstant } from "./time";

describe("zonedWallTimeToInstant", () => {
  it("interprets the wall time in the named zone and keeps its offset", () => {
    expect(zonedWallTimeToInstant("2026-10-12T09:00", "Europe/Moscow")).toEqual({
      iso: "2026-10-12T09:00:00+03:00",
      utc: "2026-10-12T06:00:00Z",
      offset: "+03:00",
      timeZone: "Europe/Moscow",
    });
  });

  it("follows DST in zones that have it", () => {
    expect(zonedWallTimeToInstant("2026-07-01T12:00", "Europe/Berlin")?.iso).toBe(
      "2026-07-01T12:00:00+02:00",
    );
    expect(zonedWallTimeToInstant("2026-12-01T12:00", "Europe/Berlin")?.iso).toBe(
      "2026-12-01T12:00:00+01:00",
    );
    expect(zonedWallTimeToInstant("2026-12-01T12:00", "America/New_York")?.utc).toBe(
      "2026-12-01T17:00:00Z",
    );
  });

  it("refuses a wall time that does not exist, malformed input and unknown zones", () => {
    expect(zonedWallTimeToInstant("2026-03-29T02:30", "Europe/Berlin")).toBeNull();
    expect(zonedWallTimeToInstant("tomorrow", "Europe/Moscow")).toBeNull();
    expect(zonedWallTimeToInstant("2026-10-12T09:00", "Mars/Olympus")).toBeNull();
  });

  it("round-trips with toWallTime", () => {
    expect(toWallTime(new Date("2026-10-12T06:00:00Z"), "Europe/Moscow")).toBe("2026-10-12T09:00");
  });
});

describe("redactSecrets", () => {
  it("masks credential-like keys at any depth", () => {
    expect(
      redactSecrets({
        session_id: "s1",
        password: "hunter2",
        nested: { csrf_token: "t", list: [{ api_key: "k", ok: 1 }] },
      }),
    ).toEqual({
      session_id: "s1",
      password: REDACTED,
      nested: { csrf_token: REDACTED, list: [{ api_key: REDACTED, ok: 1 }] },
    });
  });
});
