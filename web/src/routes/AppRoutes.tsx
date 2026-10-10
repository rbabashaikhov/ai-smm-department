import { Navigate, Outlet, Route, Routes, useLocation } from "react-router";

import { ErrorBanner } from "../components/ErrorBanner";
import { Loading } from "../components/ui";
import { useAuth } from "../features/auth/AuthProvider";
import { ActiveProjectProvider } from "../features/projects/hooks";
import { AppLayout } from "../layouts/AppLayout";
import { AttentionPage } from "../pages/AttentionPage";
import { AuditPage } from "../pages/AuditPage";
import { ContentDetailPage } from "../pages/ContentDetailPage";
import { ContentListPage } from "../pages/ContentListPage";
import { CreateContentPage } from "../pages/CreateContentPage";
import { DashboardPage } from "../pages/DashboardPage";
import { LoginPage } from "../pages/LoginPage";
import { NotFoundPage } from "../pages/NotFoundPage";
import { ProjectsPage } from "../pages/ProjectsPage";
import { PublicationDetailPage } from "../pages/PublicationDetailPage";
import { PublicationsPage } from "../pages/PublicationsPage";

/** Bootstrap must finish before any protected page renders. */
function RequireAuth() {
  const { state, retryBootstrap } = useAuth();
  const location = useLocation();

  if (state.status === "loading") return <Loading label="Проверка сессии…" />;

  if (state.status === "error") {
    return (
      <div className="login-page">
        <ErrorBanner error={state.error} onRetry={retryBootstrap} />
      </div>
    );
  }

  if (state.status === "anonymous") {
    const from = state.reason === "logout" ? undefined : location.pathname + location.search;

    return <Navigate to="/login" replace state={from ? { from } : null} />;
  }

  return (
    <ActiveProjectProvider>
      <Outlet />
    </ActiveProjectProvider>
  );
}

export function AppRoutes() {
  const { state } = useAuth();

  return (
    <Routes>
      <Route
        path="/login"
        element={state.status === "loading" ? <Loading /> : <LoginPage />}
      />
      <Route element={<RequireAuth />}>
        <Route element={<AppLayout />}>
          <Route index element={<Navigate to="/projects" replace />} />
          <Route path="/projects" element={<ProjectsPage />} />
          <Route path="/projects/:projectId" element={<DashboardPage />} />
          <Route path="/projects/:projectId/content" element={<ContentListPage />} />
          <Route path="/projects/:projectId/content/new" element={<CreateContentPage />} />
          <Route path="/projects/:projectId/publications" element={<PublicationsPage />} />
          <Route path="/projects/:projectId/attention" element={<AttentionPage />} />
          <Route path="/projects/:projectId/audit" element={<AuditPage />} />
          <Route path="/content/:contentItemId" element={<ContentDetailPage />} />
          <Route path="/publications/:publicationId" element={<PublicationDetailPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Route>
    </Routes>
  );
}
