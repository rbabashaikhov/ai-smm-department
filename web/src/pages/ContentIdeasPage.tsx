import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";

import { ConfirmDialog } from "../components/ConfirmDialog";
import { ErrorBanner } from "../components/ErrorBanner";
import { Empty, Loading, PageHeader, Pagination, StatusBadge } from "../components/ui";
import {
  isIdeaOutcomeUnknown,
  useContentIdeas,
  useRejectContentIdea,
  useUseContentIdea,
} from "../features/ideas/hooks";
import {
  useProject,
  useProjectPermissions,
  useReportActiveProject,
} from "../features/projects/hooks";
import { formatDateTime } from "../lib/time";
import { CONTENT_IDEA_STATUSES, type ContentIdea, type ContentIdeaStatus } from "../types/api";

const PAGE_SIZE = 25;

function ideaStatus(value: string | null): ContentIdeaStatus | null {
  return CONTENT_IDEA_STATUSES.includes(value as ContentIdeaStatus)
    ? (value as ContentIdeaStatus)
    : null;
}

export function ContentIdeasPage() {
  const { projectId = "" } = useParams();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [rejecting, setRejecting] = useState<ContentIdea | null>(null);
  useReportActiveProject(projectId);

  const project = useProject(projectId);
  const can = useProjectPermissions(projectId);
  const status = ideaStatus(params.get("status"));
  const offset = Math.max(0, Number(params.get("offset") ?? 0) || 0);
  const page = { limit: PAGE_SIZE, offset };
  const ideas = useContentIdeas(projectId, { status: status ? [status] : undefined }, page);
  const reject = useRejectContentIdea(projectId);
  const useIdea = useUseContentIdea(projectId);

  const update = (changes: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(changes)) {
      if (value) next.set(key, value);
      else next.delete(key);
    }
    if (!("offset" in changes)) next.delete("offset");
    setParams(next);
  };

  const handleUseProposal = (idea: ContentIdea) => {
    if (useIdea.isPending) return;
    useIdea.reset();
    useIdea.mutate(idea.id, {
      onSuccess: (result) => {
        navigate(
          `/projects/${encodeURIComponent(result.generation_job.project_id)}/generation/${encodeURIComponent(result.generation_job.id)}`,
        );
      },
    });
  };

  const confirmReject = () => {
    if (!rejecting || reject.isPending) return;
    reject.reset();
    reject.mutate(rejecting.id, { onSuccess: () => setRejecting(null) });
  };

  return (
    <>
      <PageHeader
        title="Content Ideas"
        subtitle={project.data ? project.data.display_name : undefined}
      />
      <div className="filters">
        <label>
          Status
          <select
            aria-label="Idea status filter"
            value={status ?? ""}
            onChange={(event) => update({ status: event.target.value || null })}
          >
            <option value="">все</option>
            {CONTENT_IDEA_STATUSES.map((value) => <option key={value} value={value}>{value}</option>)}
          </select>
        </label>
      </div>
      {ideas.isPending ? <Loading /> : null}
      {ideas.isError ? <ErrorBanner error={ideas.error} onRetry={() => ideas.refetch()} /> : null}
      {ideas.data?.items.length === 0 ? (
        <Empty>{status || offset ? "Идей по выбранному фильтру нет." : "Идей пока нет."}</Empty>
      ) : null}
      {ideas.data?.items.length ? (
        <>
          <table className="table">
            <thead><tr><th>Идея</th><th>Angle</th><th>Status</th><th>Создана</th><th>Действия</th></tr></thead>
            <tbody>
              {ideas.data.items.map((idea) => (
                <tr key={idea.id}>
                  <td><strong>{idea.title}</strong><div className="muted small">{idea.description}</div></td>
                  <td>{idea.angle ?? "—"}</td>
                  <td><StatusBadge status={idea.status} /></td>
                  <td>{formatDateTime(idea.created_at, project.data?.default_timezone)}</td>
                  <td>
                    {idea.status === "used" && idea.generation_job_id ? (
                      <Link to={`/projects/${encodeURIComponent(projectId)}/generation/${encodeURIComponent(idea.generation_job_id)}`}>Generation job</Link>
                    ) : null}
                    {idea.status === "proposed" && can.canEditContent ? (
                      <div className="form-actions">
                        <button type="button" className="btn-primary" onClick={() => handleUseProposal(idea)} disabled={useIdea.isPending || reject.isPending}>Создать черновик</button>
                        <button type="button" className="btn-danger" onClick={() => { reject.reset(); setRejecting(idea); }} disabled={useIdea.isPending || reject.isPending}>Отклонить</button>
                      </div>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination page={page} total={ideas.data.total} onChange={(next) => update({ offset: next.offset ? String(next.offset) : null })} />
        </>
      ) : null}
      {useIdea.isError ? <ErrorBanner error={useIdea.error} /> : null}
      {useIdea.isError && isIdeaOutcomeUnknown(useIdea.error) ? (
        <div className="banner banner-warn" role="status">Ответ на создание черновика не получен. Состояние идей перечитано с сервера; не отправляйте команду повторно, пока не проверите результат.</div>
      ) : null}
      {rejecting ? (
        <ConfirmDialog title="Отклонить идею?" confirmLabel="Отклонить" danger pending={reject.isPending} error={reject.error} onCancel={() => { if (!reject.isPending) setRejecting(null); }} onConfirm={confirmReject}>
          Идея «{rejecting.title}» будет отклонена. Черновик создан не будет.
        </ConfirmDialog>
      ) : null}
    </>
  );
}
