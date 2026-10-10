import { Link } from "react-router";

import { StatusBadge } from "../../components/ui";
import { formatDateTime } from "../../lib/time";
import type { PublicationSummary } from "../../types/api";
import { usePublication } from "./hooks";
import { PublicationActions } from "./PublicationActions";

/**
 * A list row carries no last_error (the API keeps the operational fields
 * on the detail). For a row that has had attempts, the detail is read
 * lazily and shared with the detail page's cache.
 */
function LastErrorCell({ row }: { row: PublicationSummary }) {
  const relevant =
    row.attempt_count > 0 || row.status === "failed" || row.status === "needs_review";

  if (!relevant) return <span className="muted">—</span>;

  return <LastErrorLoaded id={row.id} />;
}

function LastErrorLoaded({ id }: { id: number }) {
  const detail = usePublication(id);

  if (detail.isPending) return <span className="muted">…</span>;
  if (detail.isError) return <span className="muted">n/a</span>;
  if (!detail.data.last_error) return <span className="muted">—</span>;

  return (
    <span className="error-text" title={detail.data.last_error}>
      {detail.data.last_error.length > 80
        ? `${detail.data.last_error.slice(0, 80)}…`
        : detail.data.last_error}
    </span>
  );
}

export function PublicationTable({
  rows,
  timeZone,
  showActions = true,
}: {
  rows: PublicationSummary[];
  timeZone: string | undefined;
  showActions?: boolean;
}) {
  return (
    <table className="table">
      <thead>
        <tr>
          <th>#</th>
          <th>Title</th>
          <th>Status</th>
          <th>Format</th>
          <th>Scheduled</th>
          <th>Reviewed</th>
          <th>Series</th>
          <th>Attempts</th>
          <th>Last error</th>
          {showActions ? <th>Actions</th> : null}
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.id}>
            <td className="num">{row.id}</td>
            <td>
              <Link to={`/publications/${row.id}`}>{row.title || "(untitled)"}</Link>
              <div className="muted small">
                {row.body_chars} chars · {row.image_count} img
              </div>
            </td>
            <td>
              <StatusBadge status={row.status} />
            </td>
            <td>{row.format}</td>
            <td>{formatDateTime(row.scheduled_at, timeZone)}</td>
            <td>{row.human_reviewed ? "yes" : <span className="muted">no</span>}</td>
            <td>
              {row.series_id ? (
                <>
                  #{row.series_id} · {row.series_position ?? "?"}/{row.series_total ?? "?"}
                </>
              ) : (
                <span className="muted">—</span>
              )}
            </td>
            <td className="num">{row.attempt_count}</td>
            <td>
              <LastErrorCell row={row} />
            </td>
            {showActions ? (
              <td>
                <PublicationActions publication={row} />
              </td>
            ) : null}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
