import { Navigate, Outlet, Route, Routes, useLocation } from "react-router";

import { ErrorBanner } from "../components/ErrorBanner";
import { Loading } from "../components/ui";
import { useAuth, useMe } from "../features/auth/AuthProvider";
import { ActiveProjectProvider } from "../features/projects/hooks";
import { ProjectScope } from "../features/projects/ProjectScope";
import { AppLayout } from "../layouts/AppLayout";
import { AccountsPage } from "../pages/AccountsPage";
import { AttentionPage } from "../pages/AttentionPage";
import { AuditPage } from "../pages/AuditPage";
import { ContentDetailPage } from "../pages/ContentDetailPage";
import { ContentIdeasPage } from "../pages/ContentIdeasPage";
import { ContentListPage } from "../pages/ContentListPage";
import { CreateContentPage } from "../pages/CreateContentPage";
import { CreateProjectPage } from "../pages/CreateProjectPage";
import { CredentialsPage } from "../pages/CredentialsPage";
import { DashboardPage } from "../pages/DashboardPage";
import { GenerationJobPage } from "../pages/GenerationJobPage";
import { GenerationJobsPage } from "../pages/GenerationJobsPage";
import { KnowledgeBasePage } from "../pages/KnowledgeBasePage";
import { LoginPage } from "../pages/LoginPage";
import { NotFoundPage } from "../pages/NotFoundPage";
import { ProjectSettingsPage } from "../pages/ProjectSettingsPage";
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

/** "/": a user with exactly one project goes straight into it. */
function HomeRedirect() {
  const { projects } = useMe();
  const only = projects.length === 1 ? projects[0] : undefined;

  return (
    <Navigate to={only ? `/projects/${encodeURIComponent(only.project_id)}` : "/projects"} replace />
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
          <Route index element={<HomeRedirect />} />
          <Route path="/projects" element={<ProjectsPage />} />
          {/* Static beats dynamic in React Router; "new" is also a reserved project id. */}
          <Route path="/projects/new" element={<CreateProjectPage />} />
          <Route path="/projects/:projectId" element={<ProjectScope />}>
            <Route index element={<DashboardPage />} />
            <Route path="content" element={<ContentListPage />} />
            <Route path="content/new" element={<CreateContentPage />} />
            <Route path="ideas" element={<ContentIdeasPage />} />
            <Route path="generation" element={<GenerationJobsPage />} />
            <Route path="generation/:jobId" element={<GenerationJobPage />} />
            <Route path="publications" element={<PublicationsPage />} />
            <Route path="attention" element={<AttentionPage />} />
            <Route path="audit" element={<AuditPage />} />
            <Route path="knowledge" element={<KnowledgeBasePage />} />
            <Route path="settings" element={<ProjectSettingsPage />} />
            <Route path="accounts" element={<AccountsPage />} />
            <Route path="credentials" element={<CredentialsPage />} />
          </Route>
          <Route path="/content/:contentItemId" element={<ContentDetailPage />} />
          <Route path="/publications/:publicationId" element={<PublicationDetailPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Route>
    </Routes>
  );
}
