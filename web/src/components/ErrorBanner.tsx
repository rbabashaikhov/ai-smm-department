import { useEffect, useState } from "react";

import { ApiError, ErrorCode, isValidationError } from "../api/errors";

/** Seconds left until a 503's Retry-After has elapsed. */
export function useRetryCountdown(error: unknown): number {
  const retryAfter =
    error instanceof ApiError && error.isUnavailable ? (error.retryAfter ?? 0) : 0;
  const [left, setLeft] = useState(retryAfter);
  const [tracked, setTracked] = useState(error);

  if (tracked !== error) {
    setTracked(error);
    setLeft(retryAfter);
  }

  useEffect(() => {
    if (left <= 0) return;

    const timer = setTimeout(() => setLeft((n) => n - 1), 1000);

    return () => clearTimeout(timer);
  }, [left]);

  return left;
}

function describe(error: unknown): { title: string; message: string } {
  if (!(error instanceof ApiError)) {
    return {
      title: "Ошибка",
      message: error instanceof Error ? error.message : "Unexpected error.",
    };
  }

  switch (error.code) {
    case ErrorCode.FORBIDDEN:
      return {
        title: "Недостаточно прав",
        message: "Your role in this project does not allow this action.",
      };
    case ErrorCode.CSRF_REQUIRED:
    case ErrorCode.CSRF_INVALID:
      return {
        title: "Сессия изменилась",
        message:
          "The security token no longer matches this session (signed in again in another tab?). Reload the page.",
      };
    case ErrorCode.NOT_FOUND:
      return { title: "Не найдено", message: error.message };
    case ErrorCode.NETWORK_ERROR:
      return { title: "Нет соединения с API", message: error.message };
    default:
      if (error.isUnavailable) {
        return {
          title: "Сервис занят",
          message: `${error.message} Nothing was changed.`,
        };
      }

      return { title: `Ошибка ${error.status || ""}`.trim(), message: error.message };
  }
}

export function ErrorBanner({
  error,
  onRetry,
  retryLabel = "Retry",
}: {
  error: unknown;
  onRetry?: () => void;
  retryLabel?: string;
}) {
  const left = useRetryCountdown(error);

  if (!error) return null;

  const { title, message } = describe(error);
  const busy = error instanceof ApiError && error.isUnavailable;
  const fields = isValidationError(error) ? error.details.fields : undefined;
  const contentError = isValidationError(error) ? error.details.content_error : undefined;

  return (
    <div className={`banner ${busy ? "banner-warn" : "banner-error"}`} role="alert">
      <strong>{title}.</strong> {message}
      {fields && fields.length > 0 && (
        <ul className="field-errors">
          {fields.map((f) => (
            <li key={`${f.field}:${f.rule}`}>
              <code>{f.field}</code>: {f.rule}
            </li>
          ))}
        </ul>
      )}
      {contentError && (
        <div>
          Preflight: <code>{contentError}</code>
        </div>
      )}
      {busy && left > 0 && <div className="muted">Можно повторить через {left} с.</div>}
      {onRetry && (
        <div className="banner-actions">
          <button type="button" onClick={onRetry} disabled={busy && left > 0}>
            {retryLabel}
          </button>
        </div>
      )}
      {error instanceof ApiError && error.requestId && (
        <div className="muted small">request_id: {error.requestId}</div>
      )}
    </div>
  );
}
