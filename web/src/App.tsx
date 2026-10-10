import type { QueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { BrowserRouter } from "react-router";

import type { ApiClient } from "./api/client";
import { ApiProvider } from "./api/context";
import { AuthProvider } from "./features/auth/AuthProvider";
import { AppRoutes } from "./routes/AppRoutes";

export function Providers({
  children,
  client,
  queryClient,
}: {
  children: ReactNode;
  client?: ApiClient;
  queryClient?: QueryClient;
}) {
  return (
    <ApiProvider client={client} queryClient={queryClient}>
      <AuthProvider>{children}</AuthProvider>
    </ApiProvider>
  );
}

export function App() {
  return (
    <Providers>
      <BrowserRouter>
        <AppRoutes />
      </BrowserRouter>
    </Providers>
  );
}
