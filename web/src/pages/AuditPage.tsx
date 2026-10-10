import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useParams } from "react-router";

import { useApi } from "../api/context";
import { qk } from "../api/queryKeys";
import { ErrorBanner } from "../components/ErrorBanner";
import { Empty, Loading, PageHeader, Pagination } from "../components/ui";
import {
  useProject,
  useProjectPermissions,
  useReportActiveProject,
} from "../features/projects/hooks";
import { redactSecrets } from "../lib/redact";
import { formatDateTime } from "../lib/time";

const PAGE_SIZE = 50;

export function AuditPage() {
  const { projectId = "" } = useParams();
  const api = useApi();
  useReportActiveProject(projectId);

  const can = useProjectPermissions(projectId);
  const project = useProject(projectId);
  const [page, setPage] = useState({ limit: PAGE_SIZE, offset: 0 });
  const [action, setAction] = useState("");
  const [actionDraft, setActionDraft] = useState("");

  const list = useQuery({
    queryKey: qk.audit(projectId, page, action),
    queryFn: () => api.projects.audit(projectId, page, action || undefined),
    placeholderData: keepPreviousData,
    // Viewers and editors are refused by the backend anyway (403); the
    // page does not ask on their behalf.
    enabled: can.canReadAudit,
  });

  if (!can.canReadAudit) {
    return (
      <>
        <PageHeader title="Audit" />
        <div className="banner banner-warn">Журнал аудита доступен ролям admin и owner.</div>
      </>
    );
  }

  const tz = project.data?.default_timezone;

  return (
    <>
      <PageHeader title="Audit" subtitle="Кто что сделал в этом проекте" />
      <form
        className="filters"
        onSubmit={(e) => {
          e.preventDefault();
          setAction(actionDraft.trim());
          setPage((p) => ({ ...p, offset: 0 }));
        }}
      >
        <label>
          Action
          <input
            aria-label="Action filter"
            value={actionDraft}
            onChange={(e) => setActionDraft(e.target.value)}
            placeholder="content.approved"
          />
        </label>
        <button type="submit">Применить</button>
      </form>
      {list.isPending ? <Loading /> : null}
      {list.isError ? <ErrorBanner error={list.error} onRetry={() => list.refetch()} /> : null}
      {list.data && list.data.items.length === 0 ? <Empty>Записей нет.</Empty> : null}
      {list.data && list.data.items.length > 0 ? (
        <>
          <table className="table compact">
            <thead>
              <tr>
                <th>When</th>
                <th>Actor</th>
                <th>Action</th>
                <th>Subject</th>
                <th>Details</th>
              </tr>
            </thead>
            <tbody>
              {list.data.items.map((entry) => (
                <tr key={entry.id}>
                  <td>{formatDateTime(entry.at, tz)}</td>
                  <td>
                    <code>{entry.actor}</code>
                  </td>
                  <td>
                    <code>{entry.action}</code>
                  </td>
                  <td>
                    <code>{entry.subject ?? "—"}</code>
                  </td>
                  <td>
                    {Object.keys(entry.details).length ? (
                      <details>
                        <summary>{Object.keys(entry.details).join(", ")}</summary>
                        <pre className="json">
                          {JSON.stringify(redactSecrets(entry.details), null, 2)}
                        </pre>
                      </details>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination page={page} total={list.data.total} onChange={setPage} />
        </>
      ) : null}
    </>
  );
}
