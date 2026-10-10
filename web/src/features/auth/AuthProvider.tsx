// Session bootstrap, login and logout.
//
// The session itself is an HttpOnly cookie the page cannot read. What this
// provider holds is the *result* of GET /auth/me -- who is signed in and
// on which projects -- and whether bootstrap has finished. The CSRF token
// is kept by the ApiClient, in memory, and is not part of this state.

import { useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { useApi, useApiClient } from "../../api/context";
import { isUnauthorized } from "../../api/errors";
import { qk } from "../../api/queryKeys";
import type { MeResponse, ProjectAccess } from "../../types/api";

type AuthState =
  | { status: "loading" }
  /** `reason` "logout" means the person chose to leave: the next login,
   *  possibly by someone else, starts fresh instead of returning to the
   *  page the previous session was on. */
  | { status: "anonymous"; reason: "no_session" | "expired" | "logout" }
  | { status: "error"; error: unknown }
  | { status: "authenticated"; me: MeResponse };

interface AuthContextValue {
  state: AuthState;
  login: (email: string, password: string) => Promise<MeResponse>;
  logout: () => Promise<void>;
  retryBootstrap: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const api = useApi();
  const client = useApiClient();
  const queryClient = useQueryClient();
  const [state, setState] = useState<AuthState>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);

  const signedOut = useCallback(
    (reason: "expired" | "logout") => {
      client.clearCsrfToken();
      queryClient.clear();
      setState({ status: "anonymous", reason });
    },
    [client, queryClient],
  );

  // Bootstrap: me -> csrf -> protected routes. A 401 means "not signed in".
  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const me = await api.auth.me();
        await client.loadCsrfToken();

        if (!cancelled) setState({ status: "authenticated", me });
      } catch (error) {
        if (cancelled) return;

        if (isUnauthorized(error)) {
          setState({ status: "anonymous", reason: "no_session" });
        } else {
          setState({ status: "error", error });
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [api, client, attempt]);

  // Any later 401 (expired, idled out or revoked session) drops the user
  // back to the login page with nothing cached.
  useEffect(() => client.onUnauthorized(() => signedOut("expired")), [client, signedOut]);

  const login = useCallback(
    async (email: string, password: string) => {
      await api.auth.login(email, password);

      // The login response carries a CSRF token, but the documented flow
      // is me -> csrf, so a tab ends up in the same state whichever way it
      // got its session.
      const me = await api.auth.me();
      await client.loadCsrfToken();
      await queryClient.prefetchQuery({
        queryKey: qk.projects(),
        queryFn: () => api.projects.list(),
      });

      setState({ status: "authenticated", me });

      return me;
    },
    [api, client, queryClient],
  );

  const logout = useCallback(async () => {
    try {
      await api.auth.logout();
    } catch (error) {
      // An already-dead session is the outcome logout wanted anyway.
      if (!isUnauthorized(error)) throw error;
    }

    signedOut("logout");
  }, [api, signedOut]);

  const retryBootstrap = useCallback(() => {
    setState({ status: "loading" });
    setAttempt((n) => n + 1);
  }, []);

  const value = useMemo(
    () => ({ state, login, logout, retryBootstrap }),
    [state, login, logout, retryBootstrap],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);

  if (!value) throw new Error("useAuth must be used inside <AuthProvider>");

  return value;
}

/** The signed-in user. Only valid below <RequireAuth>. */
export function useMe(): MeResponse {
  const { state } = useAuth();

  if (state.status !== "authenticated") {
    throw new Error("useMe used outside an authenticated route");
  }

  return state.me;
}

/**
 * Whether the server accepts changes at all. Anything but an explicit
 * `true` -- not signed in, an older API without the field -- is read-only.
 */
export function useMutationsEnabled(): boolean {
  const { state } = useAuth();

  return state.status === "authenticated" && state.me.control_plane?.mutations_enabled === true;
}

export function useProjectAccess(projectId: string | undefined): ProjectAccess | undefined {
  const { state } = useAuth();

  if (state.status !== "authenticated" || !projectId) return undefined;

  return state.me.projects.find((p) => p.project_id === projectId);
}
