import { useQuery } from "@tanstack/react-query";
import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { useApi } from "../../api/context";
import { qk } from "../../api/queryKeys";
import { useMutationsEnabled, useProjectAccess } from "../auth/AuthProvider";
import { permissions, type Permissions } from "../auth/rbac";

export function useProjects() {
  const api = useApi();

  return useQuery({ queryKey: qk.projects(), queryFn: () => api.projects.list() });
}

export function useProject(projectId: string | undefined) {
  const api = useApi();

  return useQuery({
    queryKey: qk.project(projectId ?? ""),
    queryFn: () => api.projects.get(projectId!),
    enabled: Boolean(projectId),
  });
}

/**
 * UX permissions in one project. The role comes from GET /projects/{id}
 * when loaded (it is the freshest), else from /auth/me. Unknown role, or
 * a read-only deployment, means no write controls at all.
 */
export function useProjectPermissions(projectId: string | undefined): Permissions {
  const project = useProject(projectId);
  const access = useProjectAccess(projectId);
  const mutationsEnabled = useMutationsEnabled();

  return permissions(project.data?.role ?? access?.role, mutationsEnabled);
}

// -- the project the sidebar is pointing at ------------------------------
// /content/:id and /publications/:id carry no project in the URL; their
// pages report the project of the row they loaded.

interface ActiveProjectValue {
  projectId: string | null;
  setProjectId: (projectId: string | null) => void;
}

const ActiveProjectContext = createContext<ActiveProjectValue | null>(null);

export function ActiveProjectProvider({ children }: { children: ReactNode }) {
  const [projectId, setProjectId] = useState<string | null>(null);
  const value = useMemo(() => ({ projectId, setProjectId }), [projectId]);

  return (
    <ActiveProjectContext.Provider value={value}>{children}</ActiveProjectContext.Provider>
  );
}

export function useActiveProjectId(): string | null {
  return useContext(ActiveProjectContext)?.projectId ?? null;
}

/** Called by a page that knows which project it is showing. */
export function useReportActiveProject(projectId: string | undefined | null): void {
  const setProjectId = useContext(ActiveProjectContext)?.setProjectId;

  useEffect(() => {
    if (projectId && setProjectId) setProjectId(projectId);
  }, [projectId, setProjectId]);
}
