import { Link, useParams } from "react-router";

import { ErrorBanner } from "../components/ErrorBanner";
import {
  Breadcrumbs,
  Card,
  KeyValues,
  Loading,
  PageHeader,
  StatusBadge,
} from "../components/ui";
import {
  useCancelGenerationJob,
  useGenerationJob,
} from "../features/generation/hooks";
import { parseGenerationResult } from "../features/generation/result";
import {
  useProject,
  useProjectPermissions,
  useReportActiveProject,
} from "../features/projects/hooks";
import { formatDateTime } from "../lib/time";

const textParam = (value: unknown): string =>
  typeof value === "string" && value.trim() ? value : "—";

const numberParam = (value: unknown): string =>
  typeof value === "number" && Number.isFinite(value) ? String(value) : "—";

export function GenerationJobPage() {
  const { projectId = "", jobId = "" } = useParams();
  useReportActiveProject(projectId);

  const project = useProject(projectId);
  const can = useProjectPermissions(projectId);
  const jobQuery = useGenerationJob(projectId, jobId);
  const cancel = useCancelGenerationJob(projectId, jobId);

  if (jobQuery.isPending) return <Loading label="Загрузка задания…" />;
  if (jobQuery.isError) {
    return <ErrorBanner error={jobQuery.error} onRetry={() => jobQuery.refetch()} />;
  }

  const job = jobQuery.data;
  const listPath = `/projects/${encodeURIComponent(projectId)}/generation`;

  // The backend authorises from the job's own project. This comparison only
  // prevents a misleading project strip when a member hand-edits both ids.
  if (job.project_id !== projectId) {
    return (
      <div className="banner banner-warn" role="alert">
        Задание не относится к выбранному проекту. <Link to={listPath}>К списку заданий</Link>
      </div>
    );
  }

  const active = job.status === "queued" || job.status === "claimed";
  const result = parseGenerationResult(job.result);
  const tz = project.data?.default_timezone;

  return (
    <>
      <Breadcrumbs
        items={[
          { label: "Проекты", to: "/projects" },
          { label: project.data?.display_name ?? projectId, to: `/projects/${encodeURIComponent(projectId)}` },
          { label: "AI Generation", to: listPath },
          { label: job.id },
        ]}
      />
      <PageHeader
        title="Generation job"
        subtitle={<><StatusBadge status={job.status} /> · <code>{job.id}</code></>}
        actions={can.canEditContent && active ? (
          <button
            type="button"
            className="btn-danger"
            disabled={cancel.isPending}
            onClick={() => cancel.mutate()}
          >
            {cancel.isPending ? "Отмена…" : "Cancel generation"}
          </button>
        ) : null}
      />

      {jobQuery.isRefetching ? <div className="muted small">Обновление статуса…</div> : null}
      {cancel.isError ? <ErrorBanner error={cancel.error} /> : null}
      {job.status === "failed" && job.last_error ? (
        <div className="banner banner-error" role="alert">
          <strong>Generation failed.</strong> {job.last_error}
        </div>
      ) : null}

      <div className="grid-2">
        <Card title="Job">
          <KeyValues
            rows={[
              ["project", project.data?.display_name ?? job.project_id],
              ["status", <StatusBadge key="status" status={job.status} />],
              ["attempts", `${job.attempt_count} / ${job.max_attempts}`],
              ["created", formatDateTime(job.created_at, tz)],
              ["updated", formatDateTime(job.updated_at, tz)],
              ["finished", formatDateTime(job.finished_at, tz)],
            ]}
          />
        </Card>
        <Card title="Parameters">
          <KeyValues
            rows={[
              ["topic_hint", textParam(job.params.topic_hint)],
              ["series_size", numberParam(job.params.series_size)],
              ["publishing_strategy", textParam(job.params.publishing_strategy)],
            ]}
          />
        </Card>
      </div>

      {job.status === "succeeded" ? (
        <Card title="Generation result">
          <KeyValues
            rows={[
              ["series title", result.seriesTitle ?? "—"],
              ["generated drafts", result.contentItemIds?.length ?? "—"],
              ["revision count", result.revisionCount ?? "—"],
              ["tokens input", result.tokens?.input ?? "—"],
              ["tokens output", result.tokens?.output ?? "—"],
              ["tokens cached", result.tokens?.cached ?? "—"],
              ["cost USD", result.costUsd ?? "—"],
              ["duration", result.seconds === undefined ? "—" : `${result.seconds} s`],
            ]}
          />

          {result.contentItemIds === null ? (
            <div className="banner banner-warn" role="alert">
              Результат задания имеет некорректный формат. Ссылки на черновики не показаны.
            </div>
          ) : result.contentItemIds.length === 0 ? (
            <div className="banner banner-warn">Черновики не созданы.</div>
          ) : (
            <>
              <h3>Generated drafts</h3>
              <ul>
                {result.contentItemIds.map((itemId, index) => (
                  <li key={itemId}>
                    <Link to={`/content/${encodeURIComponent(itemId)}`}>
                      Draft {index + 1}: <code>{itemId}</code>
                    </Link>
                  </li>
                ))}
              </ul>
              <div className="banner banner-info" role="note">
                Перед отправкой на согласование выберите социальный аккаунт через New Revision.
                Generation UI не создаёт ревизию и не отправляет материал на review автоматически.
              </div>
            </>
          )}
        </Card>
      ) : null}
    </>
  );
}
