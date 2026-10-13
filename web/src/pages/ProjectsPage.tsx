import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";

import { useApi } from "../api/context";
import { qk } from "../api/queryKeys";
import { ErrorBanner } from "../components/ErrorBanner";
import { Empty, Loading, PageHeader } from "../components/ui";
import { useMe, useMutationsEnabled } from "../features/auth/AuthProvider";
import { canCreateProject } from "../features/auth/rbac";
import { useProjects } from "../features/projects/hooks";
import { InactiveBadge } from "../features/projects/ProjectScope";
import { formatDateTime } from "../lib/time";
import type { Project } from "../types/api";

function SettingsSummary({ project }: { project: Project }) {
  const api = useApi();
  const settings = useQuery({
    queryKey: qk.settings(project.id),
    queryFn: () => api.projects.settings(project.id),
  });

  if (settings.isPending) return <span className="muted">…</span>;
  if (settings.isError) return <span className="muted">недоступны</span>;

  const { content_config, publishing_config, brand_config, version } = settings.data;
  const keys = (o: object) => Object.keys(o).length;

  if (version === 0) return <span className="muted">не заданы (v0)</span>;

  return (
    <details>
      <summary>
        v{version}: content {keys(content_config)}, publishing {keys(publishing_config)}, brand{" "}
        {keys(brand_config)}
      </summary>
      <pre className="json">
        {JSON.stringify({ content_config, publishing_config, brand_config }, null, 2)}
      </pre>
    </details>
  );
}

/**
 * Queue counters of one project. Each row asks on its own, so one
 * project that fails to answer shows "—" and the others still render.
 */
function SummaryCells({ project }: { project: Project }) {
  const api = useApi();
  const summary = useQuery({
    queryKey: qk.operationsSummary(project.id),
    queryFn: () => api.projects.operationsSummary(project.id),
  });

  if (!summary.data) {
    const placeholder = summary.isError ? "—" : "…";

    return (
      <>
        {[0, 1, 2, 3].map((i) => (
          <td key={i} className="num muted" data-testid="summary-unavailable">
            {placeholder}
          </td>
        ))}
      </>
    );
  }

  const s = summary.data;
  const base = `/projects/${encodeURIComponent(project.id)}`;

  return (
    <>
      <td className="num">
        {s.needs_attention > 0 ? (
          <Link to={`${base}/attention`}>{s.needs_attention}</Link>
        ) : (
          s.needs_attention
        )}
      </td>
      <td className="num">{s.due_now}</td>
      <td>{formatDateTime(s.next_scheduled_at, project.default_timezone)}</td>
      <td className="num">{s.published_last_24h}</td>
    </>
  );
}

export function ProjectsPage() {
  const projects = useProjects();
  const me = useMe();
  const mutationsEnabled = useMutationsEnabled();
  const mayCreate = canCreateProject(
    me.projects.map((p) => p.role),
    mutationsEnabled,
  );

  return (
    <>
      <PageHeader
        title="Проекты"
        subtitle="Проекты, в которых у вас есть роль"
        actions={
          mayCreate ? (
            <Link className="btn" to="/projects/new">
              + Новый проект
            </Link>
          ) : null
        }
      />
      {projects.isPending ? <Loading /> : null}
      {projects.isError ? (
        <ErrorBanner error={projects.error} onRetry={() => projects.refetch()} />
      ) : null}
      {projects.data && projects.data.length === 0 ? (
        <Empty>У вас нет доступа ни к одному проекту. Обратитесь к владельцу проекта.</Empty>
      ) : null}
      {projects.data && projects.data.length > 0 ? (
        <table className="table">
          <thead>
            <tr>
              <th>Проект</th>
              <th>ID</th>
              <th>Роль</th>
              <th>Attention</th>
              <th>Due now</th>
              <th>Next scheduled</th>
              <th>Published 24h</th>
              <th>Платформа</th>
              <th>Timezone</th>
              <th>Язык</th>
              <th>Settings</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {projects.data.map((project) => (
              <tr key={project.id}>
                <td>
                  <strong>{project.display_name}</strong>
                  {project.is_active ? null : <> <InactiveBadge /></>}
                  {project.description ? (
                    <div className="muted small">{project.description}</div>
                  ) : null}
                </td>
                <td>
                  <code>{project.id}</code>
                </td>
                <td>
                  <span className="badge badge-role">{project.role}</span>
                </td>
                <SummaryCells project={project} />
                <td>{project.default_platform}</td>
                <td>{project.default_timezone}</td>
                <td>{project.default_language}</td>
                <td>
                  <SettingsSummary project={project} />
                </td>
                <td>
                  <Link className="btn" to={`/projects/${encodeURIComponent(project.id)}`}>
                    Open
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </>
  );
}
