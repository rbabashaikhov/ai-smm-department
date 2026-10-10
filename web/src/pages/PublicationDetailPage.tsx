import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router";

import { useApi } from "../api/context";
import { qk } from "../api/queryKeys";
import { ErrorBanner } from "../components/ErrorBanner";
import { SnapshotView } from "../components/SnapshotView";
import { Card, KeyValues, Loading, PageHeader, StatusBadge } from "../components/ui";
import { useProject, useReportActiveProject } from "../features/projects/hooks";
import { usePublication } from "../features/publications/hooks";
import { PublicationActions } from "../features/publications/PublicationActions";
import { formatDateTime } from "../lib/time";

const CONTENT_ITEM_REF = /^content_item:([0-9a-f-]{36})$/i;

/** Read-only view of one immutable delivery snapshot. */
export function PublicationDetailPage() {
  const { publicationId: raw = "" } = useParams();
  const publicationId = Number(raw);
  const api = useApi();
  const detail = usePublication(publicationId);
  const projectId = detail.data?.project_id;
  useReportActiveProject(projectId);

  const project = useProject(projectId);
  const preview = useQuery({
    queryKey: qk.preview(publicationId),
    queryFn: () => api.publications.preview(publicationId),
    enabled: detail.isSuccess,
  });
  const attempts = useQuery({
    queryKey: qk.attempts(publicationId),
    queryFn: () => api.publications.attempts(publicationId, { limit: 50, offset: 0 }),
    enabled: detail.isSuccess,
  });

  if (!Number.isInteger(publicationId)) {
    return <ErrorBanner error={new Error("Invalid publication id.")} />;
  }
  if (detail.isPending) return <Loading />;
  if (detail.isError) return <ErrorBanner error={detail.error} onRetry={() => detail.refetch()} />;

  const p = detail.data;
  const tz = project.data?.default_timezone;
  const contentItemRef = p.source_ref ? CONTENT_ITEM_REF.exec(p.source_ref)?.[1] : undefined;

  return (
    <>
      <PageHeader
        title={`Publication #${p.id}: ${p.title || "(untitled)"}`}
        subtitle={
          <>
            <StatusBadge status={p.status} /> · {p.platform} · {p.format} · ordinal {p.ordinal}
            {" · "}
            <Link to={`/projects/${encodeURIComponent(p.project_id)}/publications`}>
              {p.project_id}
            </Link>
          </>
        }
        actions={<PublicationActions publication={p} showPreview={false} />}
      />
      <div className="banner banner-info">
        Неизменяемый delivery snapshot. Здесь его нельзя редактировать; публикует его только
        worker, когда он scheduled и наступило время.
      </div>

      <div className="grid-2 wide-left">
        <Card title="Snapshot">
          <SnapshotView
            title={p.title}
            body={p.body}
            format={p.format}
            images={p.images}
            items={p.items}
          />
        </Card>
        <div>
          <Card title="Delivery state">
            <KeyValues
              rows={[
                ["status", <StatusBadge key="s" status={p.status} />],
                ["human_reviewed", p.human_reviewed ? "yes" : "no"],
                ["scheduled_at", formatDateTime(p.scheduled_at, tz)],
                ["next_attempt_at", formatDateTime(p.next_attempt_at, tz)],
                ["attempts", p.attempt_count],
                ["published_at", formatDateTime(p.published_at, tz)],
                ["threads_post_id", p.threads_post_id ?? "—"],
                ["editor_score", p.editor_score ?? "—"],
                [
                  "series",
                  p.series_id ? `#${p.series_id} · ${p.series_position}/${p.series_total}` : "—",
                ],
                ["idempotency_key", <code key="k">{p.idempotency_key}</code>],
                [
                  "source_ref (trace)",
                  contentItemRef ? (
                    <Link key="r" to={`/content/${contentItemRef}`}>
                      {p.source_ref}
                    </Link>
                  ) : (
                    (p.source_ref ?? "—")
                  ),
                ],
              ]}
            />
            {p.last_error ? (
              <div className="banner banner-error">
                <strong>last_error:</strong> {p.last_error}
              </div>
            ) : null}
          </Card>
          <Card title="Claim">
            <KeyValues
              rows={[
                ["claimed_by", p.claimed_by ?? "—"],
                ["claimed_at", formatDateTime(p.claimed_at, tz)],
                ["lease_expires_at", formatDateTime(p.lease_expires_at, tz)],
              ]}
            />
          </Card>
        </div>
      </div>

      <section id="preview">
        <Card title="Preview / preflight">
          <p className="muted small">
            Отчёт preflight. Ничего не отправляет и ничего не публикует.
          </p>
          {preview.isPending ? <Loading /> : null}
          {preview.isError ? (
            <ErrorBanner error={preview.error} onRetry={() => preview.refetch()} />
          ) : null}
          {preview.data ? (
            <KeyValues
              rows={[
                ["content_valid", preview.data.content_valid ? "yes" : "no"],
                ["content_error", preview.data.content_error ?? "—"],
                ["human_reviewed", preview.data.human_reviewed ? "yes" : "no"],
                ["series_ready", preview.data.series_ready ? "yes" : "no"],
                ["series_reason", preview.data.series_reason ?? "—"],
                [
                  "blocking_parts",
                  preview.data.blocking_parts.length
                    ? preview.data.blocking_parts.join(", ")
                    : "—",
                ],
                [
                  "eligible for worker when due",
                  preview.data.publishable_now ? "yes" : "no",
                ],
              ]}
            />
          ) : null}
        </Card>
      </section>

      <Card title="Delivery attempts">
        {attempts.isPending ? <Loading /> : null}
        {attempts.isError ? <ErrorBanner error={attempts.error} /> : null}
        {attempts.data && attempts.data.items.length === 0 ? (
          <p className="muted">Попыток не было.</p>
        ) : null}
        {attempts.data && attempts.data.items.length > 0 ? (
          <table className="table compact">
            <thead>
              <tr>
                <th>#</th>
                <th>Worker</th>
                <th>Phase</th>
                <th>Outcome</th>
                <th>Started</th>
                <th>Finished</th>
                <th>HTTP</th>
                <th>Error</th>
              </tr>
            </thead>
            <tbody>
              {attempts.data.items.map((a) => (
                <tr key={a.id}>
                  <td>{a.attempt_number}</td>
                  <td>
                    <code>{a.worker_id}</code>
                  </td>
                  <td>{a.phase}</td>
                  <td>{a.outcome ? <StatusBadge status={a.outcome} /> : "—"}</td>
                  <td>{formatDateTime(a.started_at, tz)}</td>
                  <td>{formatDateTime(a.finished_at, tz)}</td>
                  <td>{a.http_status ?? "—"}</td>
                  <td>
                    {a.error_type ? <code>{a.error_type}</code> : null} {a.error_message ?? ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </Card>
    </>
  );
}
