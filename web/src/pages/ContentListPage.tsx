import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";

import { useApi } from "../api/context";
import { qk } from "../api/queryKeys";
import { ErrorBanner } from "../components/ErrorBanner";
import { Empty, Loading, PageHeader, Pagination, StatusBadge, shortId } from "../components/ui";
import { useProject, useProjectPermissions, useReportActiveProject } from "../features/projects/hooks";
import { formatDateTime } from "../lib/time";
import { CONTENT_STATUSES, type ContentFilters, type ContentStatus } from "../types/api";

const PAGE_SIZE = 25;

export function ContentListPage() {
  const { projectId = "" } = useParams();
  const api = useApi();
  const [params, setParams] = useSearchParams();
  useReportActiveProject(projectId);

  const can = useProjectPermissions(projectId);
  const project = useProject(projectId);
  const tz = project.data?.default_timezone;

  const status = params.get("status") as ContentStatus | null;
  const contentType = params.get("content_type") ?? "";
  const offset = Number(params.get("offset") ?? 0) || 0;
  const [typeDraft, setTypeDraft] = useState(contentType);

  const filters: ContentFilters = {
    status: status ? [status] : undefined,
    content_type: contentType || undefined,
  };
  const page = { limit: PAGE_SIZE, offset };

  const list = useQuery({
    queryKey: qk.contentList(projectId, filters, page),
    queryFn: () => api.content.list(projectId, filters, page),
    placeholderData: keepPreviousData,
  });

  const update = (changes: Record<string, string | null>) => {
    const next = new URLSearchParams(params);

    for (const [key, value] of Object.entries(changes)) {
      if (value) next.set(key, value);
      else next.delete(key);
    }

    if (!("offset" in changes)) next.delete("offset");
    setParams(next);
  };

  return (
    <>
      <PageHeader
        title="Content"
        subtitle="Редакционные материалы: ревизии и человеческое одобрение"
        actions={
          can.canEditContent ? (
            <Link
              className="btn btn-primary"
              to={`/projects/${encodeURIComponent(projectId)}/content/new`}
            >
              Create Content
            </Link>
          ) : null
        }
      />
      <div className="filters">
        <label>
          Status
          <select
            aria-label="Status filter"
            value={status ?? ""}
            onChange={(e) => update({ status: e.target.value || null })}
          >
            <option value="">все</option>
            {CONTENT_STATUSES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            update({ content_type: typeDraft.trim() || null });
          }}
        >
          <label>
            Content type
            <input
              aria-label="Content type filter"
              value={typeDraft}
              onChange={(e) => setTypeDraft(e.target.value)}
              placeholder="post"
            />
          </label>
          <button type="submit">Применить</button>
        </form>
      </div>

      {list.isPending ? <Loading /> : null}
      {list.isError ? <ErrorBanner error={list.error} onRetry={() => list.refetch()} /> : null}
      {list.data && list.data.items.length === 0 ? <Empty>Ничего не найдено.</Empty> : null}
      {list.data && list.data.items.length > 0 ? (
        <>
          <table className="table">
            <thead>
              <tr>
                <th>Title</th>
                <th>Status</th>
                <th>Type</th>
                <th>Current revision</th>
                <th>Approved revision</th>
                <th>Version</th>
                <th>Updated</th>
              </tr>
            </thead>
            <tbody>
              {list.data.items.map((item) => (
                <tr key={item.id}>
                  <td>
                    <Link to={`/content/${item.id}`}>{item.title}</Link>
                  </td>
                  <td>
                    <StatusBadge status={item.status} />
                  </td>
                  <td>{item.content_type}</td>
                  <td>
                    <code>{shortId(item.current_revision_id)}</code>
                  </td>
                  <td>
                    {item.approved_revision_id ? (
                      <code>{shortId(item.approved_revision_id)}</code>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td className="num">{item.version}</td>
                  <td>{formatDateTime(item.updated_at, tz)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination
            page={page}
            total={list.data.total}
            onChange={(next) => update({ offset: next.offset ? String(next.offset) : null })}
          />
        </>
      ) : null}
    </>
  );
}
