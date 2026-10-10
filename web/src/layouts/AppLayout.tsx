import { useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router";

import { ErrorBanner } from "../components/ErrorBanner";
import { useAuth, useMe } from "../features/auth/AuthProvider";
import { atLeast } from "../features/auth/rbac";
import { useActiveProjectId } from "../features/projects/hooks";

export function AppLayout() {
  const me = useMe();
  const { logout } = useAuth();
  const navigate = useNavigate();
  const projectId = useActiveProjectId();
  const [logoutError, setLogoutError] = useState<unknown>(null);
  const [loggingOut, setLoggingOut] = useState(false);

  const access = me.projects.find((p) => p.project_id === projectId);
  const base = projectId ? `/projects/${encodeURIComponent(projectId)}` : null;

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
              <NavLink to={`${base}/publications`}>Publications</NavLink>
              <NavLink to={`${base}/attention`}>Attention</NavLink>
              {atLeast(access?.role, "admin") ? (
                <NavLink to={`${base}/audit`}>Audit</NavLink>
              ) : null}
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
              onChange={(event) => {
                if (event.target.value) {
                  navigate(`/projects/${encodeURIComponent(event.target.value)}`);
                }
              }}
            >
              <option value="">— выберите —</option>
              {me.projects.map((p) => (
                <option key={p.project_id} value={p.project_id}>
                  {p.display_name} ({p.role})
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
        <main className="content">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
