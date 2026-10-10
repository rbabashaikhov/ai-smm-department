import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";

import { useApi } from "../api/context";
import { qk } from "../api/queryKeys";
import { ErrorBanner } from "../components/ErrorBanner";
import { Empty, Loading, PageHeader } from "../components/ui";
import { useProjects } from "../features/projects/hooks";
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

export function ProjectsPage() {
  const projects = useProjects();

  return (
    <>
      <PageHeader title="Проекты" subtitle="Проекты, в которых у вас есть роль" />
      {projects.isPending ? <Loading /> : null}
      {projects.isError ? (
        <ErrorBanner error={projects.error} onRetry={() => projects.refetch()} />
      ) : null}
      {projects.data && projects.data.length === 0 ? (
        <Empty>У вас нет доступа ни к одному проекту.</Empty>
      ) : null}
      {projects.data && projects.data.length > 0 ? (
        <table className="table">
          <thead>
            <tr>
              <th>Проект</th>
              <th>ID</th>
              <th>Роль</th>
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
                  {project.is_active ? null : <span className="badge"> inactive</span>}
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
