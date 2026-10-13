import { Link, Outlet, useParams } from "react-router";

import { isApiError } from "../../api/errors";
import { ErrorBanner } from "../../components/ErrorBanner";
import { Loading } from "../../components/ui";
import type { Project } from "../../types/api";
import { useProject, useReportActiveProject } from "./hooks";

/** The label used wherever a project with is_active = false is shown. */
export function InactiveBadge() {
  return <span className="badge badge-inactive">неактивен</span>;
}

function ProjectStrip({ project }: { project: Project }) {
  return (
    <div className="project-strip" data-testid="project-strip">
      <strong>{project.display_name}</strong>
      <span className="muted">
        {project.id} · {project.default_platform} · {project.default_timezone}
      </span>
      <span className="badge badge-role">{project.role}</span>
      {project.is_active ? null : <InactiveBadge />}
    </div>
  );
}

/**
 * Layout of every /projects/:projectId/* page.
 *
 * Nothing below renders until GET /projects/{id} has answered for this
 * id: a stranger's or an unknown project gets one notice and no request
 * for its sub-resources. The outlet is keyed by the project, so a page
 * reached for another project starts from scratch -- no rows, paging or
 * filters of the previous one can show under the new URL.
 */
export function ProjectScope() {
  const { projectId = "" } = useParams();
  const project = useProject(projectId);
  useReportActiveProject(project.isSuccess ? projectId : null);

  if (project.isPending) return <Loading />;

  if (project.isError) {
    // The API answers 404 both for an unknown project and for one the
    // caller is not a member of, and so does this page.
    if (isApiError(project.error) && project.error.isNotFound) {
      return (
        <div className="banner banner-warn" role="alert" data-testid="project-no-access">
          <p>Проект не найден или у вас нет доступа.</p>
          <Link to="/projects">← К списку проектов</Link>
        </div>
      );
    }

    return <ErrorBanner error={project.error} onRetry={() => project.refetch()} />;
  }

  return (
    <>
      <ProjectStrip project={project.data} />
      <Outlet key={projectId} />
    </>
  );
}
