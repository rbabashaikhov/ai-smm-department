import { useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router";

import { ErrorBanner } from "../components/ErrorBanner";
import { useAuth, useMe, useMutationsEnabled } from "../features/auth/AuthProvider";
import { atLeast } from "../features/auth/rbac";
import { useActiveProjectId, useProjects } from "../features/projects/hooks";
import type { Role } from "../types/api";

interface SwitcherOption {
  id: string;
  label: string;
  role: Role;
  inactive: boolean;
}

/** Sections that exist in every project; "" is the dashboard. */
const PROJECT_SECTION =
  /^\/projects\/[^/]+(?:\/(content|generation|publications|attention|audit|settings|accounts|credentials))?\/?$/;

/**
 * Where choosing another project leads: the same section when it exists
 * there and the role allows it, else the project's dashboard. Filters
 * (the query string) are never carried over -- they belong to the
 * project they were set in.
 */
function switchTarget(pathname: string, projectId: string, role: Role): string {
  const base = `/projects/${encodeURIComponent(projectId)}`;
  const section = PROJECT_SECTION.exec(pathname)?.[1];

  if (!section) return base;
  if (section === "audit" && !atLeast(role, "admin")) return base;
  if (section === "credentials" && !atLeast(role, "owner")) return base;

  return `${base}/${section}`;
}

export function AppLayout() {
  const me = useMe();
  const mutationsEnabled = useMutationsEnabled();
  const { logout } = useAuth();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const projectId = useActiveProjectId();
  const projects = useProjects();
  const [logoutError, setLogoutError] = useState<unknown>(null);
  const [loggingOut, setLoggingOut] = useState(false);

  const access = me.projects.find((p) => p.project_id === projectId);
  const base = projectId ? `/projects/${encodeURIComponent(projectId)}` : null;

  // GET /projects knows is_active; /auth/me stands in while it loads.
  const options: SwitcherOption[] = projects.data
    ? projects.data.map((p) => ({
        id: p.id,
        label: p.display_name,
        role: p.role,
        inactive: !p.is_active,
      }))
    : me.projects.map((p) => ({
        id: p.project_id,
        label: p.display_name,
        role: p.role,
        inactive: false,
      }));

  const onLogout = async () => {
    setLoggingOut(true);
    setLogoutError(null);

    try {
      await logout();
      navigate("/login", { replace: true });
    } catch (error) {
      setLogoutError(error);
      setLoggingOut(false);
    }
  };

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">SMM Control Center</div>
        <nav aria-label="Основная навигация">
          <NavLink to="/projects" end>
            Проекты
          </NavLink>
          {base ? (
            <>
              <NavLink to={base} end>
                Dashboard
              </NavLink>
              <NavLink to={`${base}/content`}>Content</NavLink>
              <NavLink to={`${base}/generation`}>AI Generation</NavLink>
              <NavLink to={`${base}/publications`}>Publications</NavLink>
              <NavLink to={`${base}/attention`}>Attention</NavLink>
              {atLeast(access?.role, "admin") ? (
                <NavLink to={`${base}/audit`}>Audit</NavLink>
              ) : null}
              <NavLink to={`${base}/accounts`}>Аккаунты</NavLink>
              {atLeast(access?.role, "owner") ? (
                <NavLink to={`${base}/credentials`}>Учётные данные</NavLink>
              ) : null}
              <NavLink to={`${base}/settings`}>Настройки</NavLink>
            </>
          ) : (
            <div className="muted small nav-hint">Выберите проект</div>
          )}
        </nav>
        <div className="sidebar-foot muted small">
          Публикует только worker. Здесь — только очередь и редакция.
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <label className="project-switcher">
            <span className="muted small">Проект</span>
            <select
              aria-label="Project switcher"
              value={projectId ?? ""}
              disabled={options.length === 0}
              onChange={(event) => {
                const target = options.find((p) => p.id === event.target.value);

                if (target) navigate(switchTarget(pathname, target.id, target.role));
              }}
            >
              <option value="">{options.length === 0 ? "— нет проектов —" : "— выберите —"}</option>
              {options.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.label} ({p.role}){p.inactive ? " (неактивен)" : ""}
                </option>
              ))}
            </select>
          </label>
          <div className="topbar-right">
            {access ? (
              <span className="badge badge-role" title="Ваша роль в проекте">
                {access.role}
              </span>
            ) : null}
            <span className="muted">{me.user.display_name || me.user.email}</span>
            <button type="button" onClick={onLogout} disabled={loggingOut}>
              Выйти
            </button>
          </div>
        </header>
        {logoutError ? <ErrorBanner error={logoutError} /> : null}
        {mutationsEnabled ? null : (
          <div className="banner banner-info" role="status" data-testid="read-only-banner">
            <strong>Только чтение.</strong> Этот Control Center не принимает изменений: сервер
            отклоняет их для любой роли, включая owner. Просмотр, вход и выход работают.
          </div>
        )}
        <main className="content">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
