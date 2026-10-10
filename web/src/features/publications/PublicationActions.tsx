import { useState } from "react";
import { Link } from "react-router";

import type { PublicationSummary } from "../../types/api";
import { useProjectPermissions } from "../projects/hooks";
import { CancelDialog, ScheduleDialog } from "./CommandDialogs";
import { offersCommand, type PublicationCommand } from "./hooks";

/**
 * Preview / Schedule / Reschedule / Cancel. There is no publish control
 * of any kind: the worker publishes what is due, and only the worker.
 */
export function PublicationActions({
  publication,
  showPreview = true,
}: {
  publication: Pick<PublicationSummary, "id" | "project_id" | "status">;
  showPreview?: boolean;
}) {
  const can = useProjectPermissions(publication.project_id);
  const [open, setOpen] = useState<PublicationCommand | null>(null);

  const offered = (command: PublicationCommand) =>
    can.canCommandPublications && offersCommand(command, publication.status);

  return (
    <div className="button-row">
      {showPreview ? (
        <Link className="btn" to={`/publications/${publication.id}#preview`}>
          Preview
        </Link>
      ) : null}
      {offered("schedule") ? (
        <button type="button" onClick={() => setOpen("schedule")}>
          Schedule
        </button>
      ) : null}
      {offered("reschedule") ? (
        <button type="button" onClick={() => setOpen("reschedule")}>
          Reschedule
        </button>
      ) : null}
      {offered("cancel") ? (
        <button type="button" className="btn-danger" onClick={() => setOpen("cancel")}>
          Cancel
        </button>
      ) : null}

      {open === "schedule" || open === "reschedule" ? (
        <ScheduleDialog
          publicationId={publication.id}
          projectId={publication.project_id}
          command={open}
          onClose={() => setOpen(null)}
        />
      ) : null}
      {open === "cancel" ? (
        <CancelDialog
          publicationId={publication.id}
          projectId={publication.project_id}
          onClose={() => setOpen(null)}
        />
      ) : null}
    </div>
  );
}
