import { render } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router";

import { ApiClient } from "../api/client";
import { createQueryClient } from "../api/context";
import { Providers } from "../App";
import { AppRoutes } from "../routes/AppRoutes";
import type { FakeApi } from "./fakeApi";

function LocationProbe() {
  const location = useLocation();

  return <div data-testid="location" hidden>{location.pathname + location.search}</div>;
}

export function renderApp(server: FakeApi, path = "/projects") {
  const client = new ApiClient({ fetchImpl: server.fetch });
  const queryClient = createQueryClient();
  const user = userEvent.setup();

  const utils = render(
    <Providers client={client} queryClient={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <AppRoutes />
        <LocationProbe />
      </MemoryRouter>
    </Providers>,
  );

  return { ...utils, client, queryClient, user };
}

export const currentPath = () =>
  document.querySelector('[data-testid="location"]')?.textContent ?? "";
