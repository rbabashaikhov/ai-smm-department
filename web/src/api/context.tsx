import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createContext, useContext, useState, type ReactNode } from "react";

import { ApiClient } from "./client";
import { createEndpoints, type Endpoints } from "./endpoints";
import { ApiError } from "./errors";

interface ApiContextValue {
  client: ApiClient;
  api: Endpoints;
}

const ApiContext = createContext<ApiContextValue | null>(null);

const MAX_QUERY_RETRIES = 2;

/**
 * Reads retry a 503 (honouring Retry-After) and a network failure, and
 * nothing else: a 4xx will not change by asking again.
 *
 * Mutations never retry automatically. A consequential command is repeated
 * only by a person pressing the button again.
 */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 15_000,
        retry: (failureCount, error) => {
          if (failureCount >= MAX_QUERY_RETRIES) return false;
          if (!(error instanceof ApiError)) return false;

          return error.status === 503 || error.status === 0;
        },
        retryDelay: (attempt, error) => {
          if (error instanceof ApiError && error.retryAfter !== null) {
            return error.retryAfter * 1000;
          }

          return Math.min(1000 * 2 ** attempt, 8000);
        },
      },
      mutations: {
        retry: false,
      },
    },
  });
}

export function ApiProvider({
  children,
  client,
  queryClient,
}: {
  children: ReactNode;
  client?: ApiClient;
  queryClient?: QueryClient;
}) {
  const [value] = useState<ApiContextValue>(() => {
    const resolved =
      client ?? new ApiClient({ baseUrl: import.meta.env.VITE_API_BASE_URL ?? "" });

    return { client: resolved, api: createEndpoints(resolved) };
  });
  const [qc] = useState(() => queryClient ?? createQueryClient());

  return (
    <ApiContext.Provider value={value}>
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    </ApiContext.Provider>
  );
}

function useApiContext(): ApiContextValue {
  const value = useContext(ApiContext);

  if (!value) throw new Error("useApi must be used inside <ApiProvider>");

  return value;
}

export const useApi = (): Endpoints => useApiContext().api;
export const useApiClient = (): ApiClient => useApiContext().client;
