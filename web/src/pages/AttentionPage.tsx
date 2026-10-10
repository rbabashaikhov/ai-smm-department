import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useParams } from "react-router";

import { useApi } from "../api/context";
import { qk } from "../api/queryKeys";
import { ErrorBanner } from "../components/ErrorBanner";
import { Empty, Loading, PageHeader, Pagination } from "../components/ui";
import { useProject, useReportActiveProject } from "../features/projects/hooks";
import { PublicationTable } from "../features/publications/PublicationTable";

const PAGE_SIZE = 25;

/** failed and needs_review rows. Reading this list retries nothing. */
export function AttentionPage() {
  const { projectId = "" } = useParams();
  const api = useApi();
  useReportActiveProject(projectId);

  const project = useProject(projectId);
  const [page, setPage] = useState({ limit: PAGE_SIZE, offset: 0 });
  const list = useQuery({
    queryKey: qk.attention(projectId, page),
    queryFn: () => api.projects.attention(projectId, page),
    placeholderData: keepPreviousData,
  });

  return (
    <>
      <PageHeader
        title="Attention"
        subtitle="failed и needs_review. Просмотр ничего не перезапускает."
      />
      <div className="banner banner-info">
        <strong>needs_review</strong>: исход отправки неизвестен — решает человек, проверивший
        реальный аккаунт Threads (<code>ai-smm reconcile</code> в CLI). Reconcile в этом UI нет.
      </div>
      {list.isPending ? <Loading /> : null}
      {list.isError ? <ErrorBanner error={list.error} onRetry={() => list.refetch()} /> : null}
      {list.data && list.data.items.length === 0 ? <Empty>Нет проблемных публикаций.</Empty> : null}
      {list.data && list.data.items.length > 0 ? (
        <>
          <PublicationTable rows={list.data.items} timeZone={project.data?.default_timezone} />
          <Pagination page={page} total={list.data.total} onChange={setPage} />
        </>
      ) : null}
    </>
  );
}
