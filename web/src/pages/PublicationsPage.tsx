import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useParams, useSearchParams } from "react-router";

import { useApi } from "../api/context";
import { qk } from "../api/queryKeys";
import { ErrorBanner } from "../components/ErrorBanner";
import { Empty, Loading, PageHeader, Pagination } from "../components/ui";
import { useProject, useReportActiveProject } from "../features/projects/hooks";
import { PublicationTable } from "../features/publications/PublicationTable";
import {
  CONTENT_FORMATS,
  PUBLICATION_STATUSES,
  type PublicationFilters,
  type PublicationStatus,
} from "../types/api";

const PAGE_SIZE = 25;

export function PublicationsPage() {
  const { projectId = "" } = useParams();
  const api = useApi();
  const [params, setParams] = useSearchParams();
  useReportActiveProject(projectId);

  const project = useProject(projectId);
  const status = params.get("status") as PublicationStatus | null;
  const format = params.get("format") ?? "";
  const reviewed = params.get("human_reviewed");
  const seriesId = params.get("series_id");
  const offset = Number(params.get("offset") ?? 0) || 0;
  const [seriesDraft, setSeriesDraft] = useState(seriesId ?? "");

  const filters: PublicationFilters = {
    status: status ? [status] : undefined,
    format: format || undefined,
    human_reviewed: reviewed === null ? undefined : reviewed === "true",
    series_id: seriesId ? Number(seriesId) : undefined,
  };
  const page = { limit: PAGE_SIZE, offset };

  const list = useQuery({
    queryKey: qk.publicationList(projectId, filters, page),
    queryFn: () => api.publications.list(projectId, filters, page),
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
        title="Publications"
        subtitle="Delivery snapshots в очереди. Отправляет их только worker."
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
            {PUBLICATION_STATUSES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <label>
          Format
          <select
            aria-label="Format filter"
            value={format}
            onChange={(e) => update({ format: e.target.value || null })}
          >
            <option value="">все</option>
            {CONTENT_FORMATS.map((f) => (
              <option key={f} value={f}>
                {f}
              </option>
            ))}
          </select>
        </label>
        <label>
          Human reviewed
          <select
            aria-label="Reviewed filter"
            value={reviewed ?? ""}
            onChange={(e) => update({ human_reviewed: e.target.value || null })}
          >
            <option value="">все</option>
            <option value="true">yes</option>
            <option value="false">no</option>
          </select>
        </label>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            update({ series_id: /^\d+$/.test(seriesDraft.trim()) ? seriesDraft.trim() : null });
          }}
        >
          <label>
            Series id
            <input
              aria-label="Series filter"
              inputMode="numeric"
              value={seriesDraft}
              onChange={(e) => setSeriesDraft(e.target.value)}
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
          <PublicationTable rows={list.data.items} timeZone={project.data?.default_timezone} />
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
