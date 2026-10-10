import { useEffect, useId, useRef, type ReactNode } from "react";

import { ErrorBanner, useRetryCountdown } from "./ErrorBanner";

/**
 * Every consequential command goes through this dialog. The confirm
 * button is the only thing that sends the request; there is no optimistic
 * update, and a failure leaves the dialog open with the server's answer.
 */
export function ConfirmDialog({
  title,
  children,
  confirmLabel,
  onConfirm,
  onCancel,
  pending = false,
  error,
  confirmDisabled = false,
  danger = false,
}: {
  title: string;
  children: ReactNode;
  confirmLabel: string;
  onConfirm: () => void;
  onCancel: () => void;
  pending?: boolean;
  error?: unknown;
  confirmDisabled?: boolean;
  danger?: boolean;
}) {
  const titleId = useId();
  // After a 503 the same command may be sent again only once the server's
  // Retry-After has passed, and only by pressing the button.
  const retryIn = useRetryCountdown(error);
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    cancelRef.current?.focus();
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !pending) onCancel();
    };

    window.addEventListener("keydown", onKey);

    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel, pending]);

  return (
    <div className="modal-backdrop">
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <h2 id={titleId}>{title}</h2>
        <div className="modal-body">{children}</div>
        {error ? <ErrorBanner error={error} /> : null}
        <div className="modal-actions">
          <button type="button" ref={cancelRef} onClick={onCancel} disabled={pending}>
            Отмена
          </button>
          <button
            type="button"
            className={danger ? "btn-danger" : "btn-primary"}
            onClick={onConfirm}
            disabled={pending || confirmDisabled || retryIn > 0}
          >
            {pending ? "Отправка…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
