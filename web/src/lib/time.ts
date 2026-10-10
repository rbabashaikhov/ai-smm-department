// Time handling for scheduling.
//
// The API refuses a naive datetime, and so does this module: a wall-clock
// time typed by an operator is always interpreted in an explicitly named
// IANA zone (the project's default_timezone), and leaves as an ISO 8601
// string carrying that zone's offset at that instant.

const pad = (n: number, width = 2) => String(Math.abs(n)).padStart(width, "0");

/** Offset of `timeZone` at `instant`, in minutes east of UTC. */
export function zoneOffsetMinutes(timeZone: string, instant: Date): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).formatToParts(instant);

  const get = (type: string) => Number(parts.find((p) => p.type === type)?.value);
  const asUtc = Date.UTC(
    get("year"),
    get("month") - 1,
    get("day"),
    get("hour"),
    get("minute"),
    get("second"),
  );

  return Math.round((asUtc - Math.floor(instant.getTime() / 1000) * 1000) / 60000);
}

export function formatOffset(minutes: number): string {
  const sign = minutes < 0 ? "-" : "+";

  return `${sign}${pad(Math.trunc(Math.abs(minutes) / 60))}:${pad(Math.abs(minutes) % 60)}`;
}

export interface ZonedInstant {
  /** e.g. 2026-10-12T09:00:00+03:00 -- what is sent to the API. */
  iso: string;
  /** The same instant in UTC, for the confirmation text. */
  utc: string;
  offset: string;
  timeZone: string;
}

const WALL_TIME = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/;

/**
 * Interpret `wallTime` ("YYYY-MM-DDTHH:mm", as produced by
 * <input type="datetime-local">) in `timeZone`.
 *
 * Returns null for a malformed value, an unknown zone, or a wall time that
 * does not exist in that zone (the hour skipped by a DST change) -- the
 * operator must pick a real time rather than have one guessed.
 */
export function zonedWallTimeToInstant(
  wallTime: string,
  timeZone: string,
): ZonedInstant | null {
  const match = WALL_TIME.exec(wallTime);

  if (!match) return null;

  const [, y, mo, d, h, mi, s] = match;
  const wallAsUtc = Date.UTC(
    Number(y),
    Number(mo) - 1,
    Number(d),
    Number(h),
    Number(mi),
    Number(s ?? 0),
  );

  let offset: number;

  try {
    // Two passes settle the offset across a DST boundary.
    offset = zoneOffsetMinutes(timeZone, new Date(wallAsUtc));
    offset = zoneOffsetMinutes(timeZone, new Date(wallAsUtc - offset * 60000));
  } catch {
    return null;
  }

  const instant = new Date(wallAsUtc - offset * 60000);

  // Round trip: a skipped wall time maps to a different wall time.
  if (toWallTime(instant, timeZone) !== `${y}-${mo}-${d}T${h}:${mi}`) return null;

  const offsetText = formatOffset(offset);

  return {
    iso: `${y}-${mo}-${d}T${h}:${mi}:${s ?? "00"}${offsetText}`,
    utc: instant.toISOString().replace(".000Z", "Z"),
    offset: offsetText,
    timeZone,
  };
}

/** "YYYY-MM-DDTHH:mm" of `instant` in `timeZone`, for datetime-local. */
export function toWallTime(instant: Date, timeZone: string): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).formatToParts(instant);

  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "";

  return `${get("year")}-${get("month")}-${get("day")}T${get("hour")}:${get("minute")}`;
}

export function isValidTimeZone(timeZone: string): boolean {
  try {
    new Intl.DateTimeFormat("en-US", { timeZone });

    return true;
  } catch {
    return false;
  }
}

/** A timestamp from the API, shown in an explicit zone with its offset. */
export function formatDateTime(value: string | null | undefined, timeZone?: string): string {
  if (!value) return "—";

  const date = new Date(value);

  if (Number.isNaN(date.getTime())) return value;

  const zone = timeZone && isValidTimeZone(timeZone) ? timeZone : undefined;
  const text = new Intl.DateTimeFormat("ru-RU", {
    timeZone: zone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(date);

  if (!zone) return text;

  return `${text} (UTC${formatOffset(zoneOffsetMinutes(zone, date))})`;
}

export function browserTimeZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone;
}
