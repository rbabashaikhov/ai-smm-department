import { useState, type FormEvent } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";

import { ErrorBanner } from "../components/ErrorBanner";
import { Card, Empty, Loading, PageHeader, Pagination, StatusBadge } from "../components/ui";
import {
  isGenerationOutcomeUnknown,
  useCreateGenerationJob,
  useGenerationJobs,
} from "../features/generation/hooks";
import {
  useProject,
  useProjectPermissions,
  useReportActiveProject,
} from "../features/projects/hooks";
import { formatDateTime } from "../lib/time";
import {
  GENERATION_JOB_STATUSES,
  type GenerationJobStatus,
} from "../types/api";

const PAGE_SIZE = 25;

function generationStatus(value: string | null): GenerationJobStatus | null {
  return GENERATION_JOB_STATUSES.includes(value as GenerationJobStatus)
    ? (value as GenerationJobStatus)
    : null;
}

export function GenerationJobsPage() {
  const { projectId = "" } = useParams();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  useReportActiveProject(projectId);

  const project = useProject(projectId);
  const can = useProjectPermissions(projectId);
  const status = generationStatus(params.get("status"));
  const offset = Math.max(0, Number(params.get("offset") ?? 0) || 0);
  const page = { limit: PAGE_SIZE, offset };
  const filters = { status: status ? [status] : undefined };
  const jobs = useGenerationJobs(projectId, filters, page);
  const create = useCreateGenerationJob(projectId);

  const [topicHint, setTopicHint] = useState("");
  const [seriesSize, setSeriesSize] = useState("3");
  const [validationError, setValidationError] = useState<string | null>(null);

  const update = (changes: Record<string, string | null>) => {
    const next = new URLSearchParams(params);

    for (const [key, value] of Object.entries(changes)) {
      if (value) next.set(key, value);
      else next.delete(key);
    }

    if (!("offset" in changes)) next.delete("offset");
    setParams(next);
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (create.isPending) return;

    const size = Number(seriesSize);
    const topic = topicHint.trim();

    if (!Number.isInteger(size) || size < 1 || size > 5) {
      setValidationError("Series size должен быть целым числом от 1 до 5.");
      return;
    }
    if (topic.length > 500) {
      setValidationError("Topic hint не должен превышать 500 символов.");
      return;
    }

    setValidationError(null);
    create.reset();
    create.mutate(
      {
        series_size: size,
        publishing_strategy: "standalone_series",
        ...(topic ? { topic_hint: topic } : {}),
      },
      {
        onSuccess: (job) => {
          navigate(
            `/projects/${encodeURIComponent(job.project_id)}/generation/${encodeURIComponent(job.id)}`,
          );
        },
      },
    );
  };

  return (
    <>
      <PageHeader
        title="AI Generation"
        subtitle={`Agent → Draft${project.data ? ` · ${project.data.display_name}` : ""}`}
      />

      {can.canEditContent ? (
        <Card title="Запустить генерацию">
          <form className="revision-form" onSubmit={submit} aria-label="Create generation job">
            <label>
              Topic hint
              <textarea
                rows={3}
                value={topicHint}
                onChange={(event) => setTopicHint(event.target.value)}
                maxLength={500}
                placeholder="Необязательная тема или направление"
              />
            </label>
            <div className="muted small">{topicHint.length} / 500</div>
            <div className="form-row">
              <label>
                Series size
                <input
                  aria-label="Series size"
                  type="number"
                  min={1}
                  max={5}
                  step={1}
                  required
                  value={seriesSize}
                  onChange={(event) => setSeriesSize(event.target.value)}
                />
              </label>
              <label className="grow">
                Publishing strategy
                <select
                  aria-label="Publishing strategy"
                  value="standalone_series"
                  disabled
                >
                  <option value="standalone_series">standalone_series</option>
                </select>
              </label>
            </div>
            {validationError ? (
              <div className="banner banner-error" role="alert">
                {validationError}
              </div>
            ) : null}
            {create.isError ? <ErrorBanner error={create.error} /> : null}
            {create.isError && isGenerationOutcomeUnknown(create.error) ? (
              <div className="banner banner-warn" role="status">
                Ответ сервера не получен, поэтому неизвестно, было ли задание создано. Не
                повторяйте запрос автоматически — проверьте список заданий ниже.
              </div>
            ) : null}
            <div className="form-actions">
              <button type="submit" className="btn-primary" disabled={create.isPending}>
                {create.isPending ? "Запуск…" : "Generate"}
              </button>
            </div>
          </form>
        </Card>
      ) : (
        <div className="banner banner-info">
          {can.readOnly
            ? "Control Center работает только на чтение. Список заданий доступен для просмотра."
            : "Запустить или отменить генерацию может только владелец проекта (owner)."}
        </div>
      )}

      <h2>Generation jobs</h2>
      <div className="filters">
        <label>
          Status
          <select
            aria-label="Generation status filter"
            value={status ?? ""}
            onChange={(event) => update({ status: event.target.value || null })}
          >
            <option value="">все</option>
            {GENERATION_JOB_STATUSES.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>
      </div>

      {jobs.isPending ? <Loading /> : null}
      {jobs.isError ? <ErrorBanner error={jobs.error} onRetry={() => jobs.refetch()} /> : null}
      {jobs.data && jobs.data.items.length === 0 ? (
        <Empty>{status || offset ? "Заданий по выбранному фильтру нет." : "Заданий пока нет."}</Empty>
      ) : null}
      {jobs.data && jobs.data.items.length > 0 ? (
        <>
          <table className="table">
            <thead>
              <tr>
                <th>Job</th>
                <th>Status</th>
                <th>Topic</th>
                <th>Attempts</th>
                <th>Created</th>
                <th>Updated</th>
              </tr>
            </thead>
            <tbody>
              {jobs.data.items.map((job) => (
                <tr key={job.id}>
                  <td>
                    <Link
                      to={`/projects/${encodeURIComponent(projectId)}/generation/${encodeURIComponent(job.id)}`}
                    >
                      <code>{job.id}</code>
                    </Link>
                  </td>
                  <td><StatusBadge status={job.status} /></td>
                  <td>{typeof job.params.topic_hint === "string" ? job.params.topic_hint : "—"}</td>
                  <td>{job.attempt_count} / {job.max_attempts}</td>
                  <td>{formatDateTime(job.created_at, project.data?.default_timezone)}</td>
                  <td>{formatDateTime(job.updated_at, project.data?.default_timezone)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination
            page={page}
            total={jobs.data.total}
            onChange={(next) => update({ offset: next.offset ? String(next.offset) : null })}
          />
        </>
      ) : null}
    </>
  );
}
