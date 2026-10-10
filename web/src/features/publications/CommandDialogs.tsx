import { useEffect, useState } from "react";

import { isInvalidStateTransition } from "../../api/errors";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { ErrorBanner } from "../../components/ErrorBanner";
import { SnapshotView } from "../../components/SnapshotView";
import { KeyValues, Loading, StatusBadge } from "../../components/ui";
import {
  browserTimeZone,
  formatDateTime,
  isValidTimeZone,
  toWallTime,
  zonedWallTimeToInstant,
} from "../../lib/time";
import type { PublicationDetail } from "../../types/api";
import { useProject } from "../projects/hooks";
import { usePublication, usePublicationCommand } from "./hooks";

/** The current time, refreshed while a dialog stays open. */
function useNow(intervalMs: number): number {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), intervalMs);

    return () => clearInterval(timer);
  }, [intervalMs]);

  return now;
}

function defaultWallTime(
  detail: PublicationDetail | undefined,
  timeZone: string,
  now: number,
): string {
  if (detail?.scheduled_at) return toWallTime(new Date(detail.scheduled_at), timeZone);

  const inOneHour = new Date(now + 60 * 60 * 1000);
  inOneHour.setUTCMinutes(0, 0, 0);

  return toWallTime(inOneHour, timeZone);
}

function SnapshotSummary({ detail }: { detail: PublicationDetail }) {
  return (
    <>
      <KeyValues
        rows={[
          ["publication", `#${detail.id} · ordinal ${detail.ordinal} · ${detail.platform}`],
          ["status", <StatusBadge key="s" status={detail.status} />],
          ["human_reviewed", detail.human_reviewed ? "yes" : "no"],
          ["idempotency_key", <code key="k">{detail.idempotency_key}</code>],
        ]}
      />
      <SnapshotView
        title={detail.title}
        body={detail.body}
        format={detail.format}
        images={detail.images}
        items={detail.items}
      />
    </>
  );
}

/**
 * Schedule or reschedule. The dialog re-reads the Publication and shows
 * the exact snapshot that will be queued; the time is entered in the
 * project's timezone and sent with that zone's explicit offset.
 */
export function ScheduleDialog({
  publicationId,
  projectId,
  command,
  onClose,
}: {
  publicationId: number;
  projectId: string;
  command: "schedule" | "reschedule";
  onClose: () => void;
}) {
  const detail = usePublication(publicationId, { fresh: true });
  const project = useProject(projectId);
  const mutation = usePublicationCommand(publicationId, projectId);
  const timeZone = project.data?.default_timezone ?? "";
  const zoneOk = isValidTimeZone(timeZone);
  const [wallTime, setWallTime] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [resetAttempts, setResetAttempts] = useState(false);
  const now = useNow(15_000);

  const ready = detail.isSuccess && !detail.isFetching && project.isSuccess && zoneOk;
  const value = wallTime ?? (ready ? defaultWallTime(detail.data, timeZone, now) : "");
  const instant = ready && value ? zonedWallTimeToInstant(value, timeZone) : null;
  const inPast = instant ? Date.parse(instant.utc) <= now : false;
  const browserZone = browserTimeZone();

  const title = command === "schedule" ? "Schedule publication" : "Reschedule publication";

  return (
    <ConfirmDialog
      title={title}
      confirmLabel={command === "schedule" ? "Confirm schedule" : "Confirm reschedule"}
      pending={mutation.isPending}
      error={mutation.error}
      confirmDisabled={!ready || !instant}
      onCancel={onClose}
      onConfirm={() => {
        if (!instant) return;

        mutation.mutate(
          {
            command,
            body: {
              scheduled_at: instant.iso,
              ...(note.trim() ? { note: note.trim() } : {}),
              ...(resetAttempts ? { reset_attempts: true } : {}),
            },
          },
          { onSuccess: onClose },
        );
      }}
    >
      {detail.isPending || project.isPending ? <Loading label="Загрузка snapshot…" /> : null}
      {detail.isError ? <ErrorBanner error={detail.error} /> : null}
      {project.isSuccess && !zoneOk ? (
        <div className="banner banner-error">
          Timezone проекта «{timeZone}» не распознан браузером — постановка недоступна.
        </div>
      ) : null}
      {detail.data ? (
        <>
          <p className="small">
            В очередь будет поставлен именно этот неизменяемый snapshot. Публикует его worker,
            когда наступит время.
          </p>
          <SnapshotSummary detail={detail.data} />
        </>
      ) : null}
      {ready ? (
        <div className="schedule-time">
          <label>
            Дата и время ({timeZone})
            <input
              type="datetime-local"
              aria-label="Scheduled time"
              value={value}
              onChange={(e) => setWallTime(e.target.value)}
              required
            />
          </label>
          <div className="small">
            Project timezone: <strong>{timeZone}</strong>
          </div>
          {instant ? (
            <KeyValues
              rows={[
                ["будет отправлено", <code key="iso">{instant.iso}</code>],
                ["UTC", <code key="utc">{instant.utc}</code>],
                ...(browserZone !== timeZone
                  ? ([
                      [
                        `в вашем браузере (${browserZone})`,
                        formatDateTime(instant.utc, browserZone),
                      ],
                    ] as [string, string][])
                  : []),
              ]}
            />
          ) : (
            <div className="banner banner-error">
              Такого времени нет в {timeZone} (переход на летнее/зимнее время?) или поле пустое.
            </div>
          )}
          {inPast ? (
            <div className="banner banner-warn">
              Время в прошлом: публикация станет due немедленно.
            </div>
          ) : null}
          {detail.data?.status === "failed" ? (
            <label className="checkbox">
              <input
                type="checkbox"
                checked={resetAttempts}
                onChange={(e) => setResetAttempts(e.target.checked)}
              />
              Сбросить счётчик попыток (reset_attempts)
            </label>
          ) : null}
          <label>
            Note (optional)
            <input value={note} onChange={(e) => setNote(e.target.value)} maxLength={500} />
          </label>
        </div>
      ) : null}
      {isInvalidStateTransition(mutation.error) ? (
        <div className="small muted">
          Текущий статус: {mutation.error.details.status}; разрешено из:{" "}
          {mutation.error.details.allowed_from.join(", ")}.
        </div>
      ) : null}
    </ConfirmDialog>
  );
}

export function CancelDialog({
  publicationId,
  projectId,
  onClose,
}: {
  publicationId: number;
  projectId: string;
  onClose: () => void;
}) {
  const detail = usePublication(publicationId, { fresh: true });
  const mutation = usePublicationCommand(publicationId, projectId);
  const [note, setNote] = useState("");

  return (
    <ConfirmDialog
      title="Cancel publication"
      confirmLabel="Cancel publication for good"
      danger
      pending={mutation.isPending}
      error={mutation.error}
      confirmDisabled={!detail.isSuccess || detail.isFetching}
      onCancel={onClose}
      onConfirm={() =>
        mutation.mutate(
          { command: "cancel", body: note.trim() ? { note: note.trim() } : {} },
          { onSuccess: onClose },
        )
      }
    >
      <p className="small">
        <strong>cancelled</strong> — терминальный статус. Этот snapshot больше не будет
        отправлен. Чтобы доставить другую ревизию, её нужно материализовать заново.
      </p>
      {detail.isPending ? <Loading /> : null}
      {detail.isError ? <ErrorBanner error={detail.error} /> : null}
      {detail.data ? <SnapshotSummary detail={detail.data} /> : null}
      <label>
        Note (optional)
        <input value={note} onChange={(e) => setNote(e.target.value)} maxLength={500} />
      </label>
    </ConfirmDialog>
  );
}
