import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Link, useParams } from "react-router";

import { useApi } from "../api/context";
import { qk } from "../api/queryKeys";
import { ErrorBanner } from "../components/ErrorBanner";
import { Card, Loading, PageHeader, StatusBadge } from "../components/ui";
import { useProject, useReportActiveProject } from "../features/projects/hooks";
import { formatDateTime } from "../lib/time";
import { CONTENT_STATUSES, PUBLICATION_STATUSES, type OperationsSummary } from "../types/api";

function Metric({ label, value, to }: { label: string; value: ReactNode; to?: string }) {
  return (
    <div className="metric">
      <div className="metric-label">{label}</div>
      <div className="metric-value">{to ? <Link to={to}>{value}</Link> : value}</div>
    </div>
  );
}

/**
 * What the worker will do with a due publication, as the API reports it.
 *
 * LIVE / DRY RUN only when the API names a verified source. Otherwise
 * UNKNOWN -- and never derived from api_dry_run, which is the API
 * process's own setting and says nothing about the worker.
 */
function WorkerMode({ summary }: { summary: OperationsSummary }) {
  const apiDryRun = (
    <div className="muted small">
      AI_SMM_DRY_RUN процесса API: <code>{String(summary.api_dry_run)}</code> — относится только к
      API, не к worker.
    </div>
  );

  if (summary.worker_mode === "live") {
    return (
      <div className="banner banner-error" role="status" data-testid="worker-mode">
        Worker: <strong>LIVE</strong> — запланированные публикации отправляются в Threads.
        {summary.worker_mode_source ? ` Источник: ${summary.worker_mode_source}.` : null}
        {apiDryRun}
      </div>
    );
  }

  if (summary.worker_mode === "dry_run") {
    return (
      <div className="banner banner-info" role="status" data-testid="worker-mode">
        Worker: <strong>DRY RUN</strong> — worker ничего не отправляет в Threads.
        {summary.worker_mode_source ? ` Источник: ${summary.worker_mode_source}.` : null}
        {apiDryRun}
      </div>
    );
  }

  return (
    <div className="banner banner-warn" role="status" data-testid="worker-mode">
      Режим worker: <strong>UNKNOWN</strong>. Control Center не получает режим worker из
      проверяемого источника. Исходите из того, что запланированная публикация может быть
      отправлена в Threads.
      {apiDryRun}
    </div>
  );
}

/**
 * Per-status totals of content items, from the existing list endpoint
 * (limit=1 per status; `total` is the count the filter matches).
 */
function useContentStatusTotals(projectId: string) {
  const api = useApi();

  return useQuery({
    queryKey: qk.contentStatusTotals(projectId),
    queryFn: async () => {
      const pages = await Promise.all(
        CONTENT_STATUSES.map((status) =>
          api.content.list(projectId, { status: [status] }, { limit: 1, offset: 0 }),
        ),
      );

      return Object.fromEntries(
        CONTENT_STATUSES.map((status, index) => [status, pages[index]?.total ?? 0]),
      ) as Record<string, number>;
    },
  });
}

export function DashboardPage() {
  const { projectId = "" } = useParams();
  const api = useApi();
  useReportActiveProject(projectId);

  const project = useProject(projectId);
  const summary = useQuery({
    queryKey: qk.operationsSummary(projectId),
    queryFn: () => api.projects.operationsSummary(projectId),
  });
  const content = useContentStatusTotals(projectId);
  const tz = project.data?.default_timezone;
  const base = `/projects/${encodeURIComponent(projectId)}`;

  return (
    <>
      <PageHeader
        title={project.data?.display_name ?? projectId}
        subtitle={
          project.data
            ? `${project.data.id} · ${project.data.default_platform} · ${project.data.default_timezone} · роль: ${project.data.role}`
            : null
        }
      />
      {project.isError ? <ErrorBanner error={project.error} /> : null}

      {summary.isPending ? <Loading /> : null}
      {summary.isError ? (
        <ErrorBanner error={summary.error} onRetry={() => summary.refetch()} />
      ) : null}
      {summary.data ? (
        <>
          <WorkerMode summary={summary.data} />
          <div className="metrics">
            <Metric label="Publications total" value={summary.data.total} to={`${base}/publications`} />
            <Metric label="Due now" value={summary.data.due_now} />
            <Metric
              label="Needs attention"
              value={summary.data.needs_attention}
              to={`${base}/attention`}
            />
            <Metric label="Published, last 24h" value={summary.data.published_last_24h} />
            <Metric label="Next scheduled" value={formatDateTime(summary.data.next_scheduled_at, tz)} />
            <Metric label="Last published" value={formatDateTime(summary.data.last_published_at, tz)} />
            <Metric
              label="Series active / total"
              value={`${summary.data.series_active} / ${summary.data.series_total}`}
            />
          </div>
          <div className="grid-2">
            <Card title="Publications by status">
              <table className="table compact">
                <tbody>
                  {PUBLICATION_STATUSES.map((status) => (
                    <tr key={status}>
                      <td>
                        <StatusBadge status={status} />
                      </td>
                      <td className="num">
                        <Link to={`${base}/publications?status=${status}`}>
                          {summary.data.by_status[status] ?? 0}
                        </Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
            <Card title="Content by status">
              {content.isPending ? <Loading /> : null}
              {content.isError ? <ErrorBanner error={content.error} /> : null}
              {content.data ? (
                <table className="table compact">
                  <tbody>
                    {CONTENT_STATUSES.map((status) => (
                      <tr key={status}>
                        <td>
                          <StatusBadge status={status} />
                        </td>
                        <td className="num">
                          <Link to={`${base}/content?status=${status}`}>
                            {content.data[status] ?? 0}
                          </Link>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : null}
            </Card>
          </div>
        </>
      ) : null}
    </>
  );
}
