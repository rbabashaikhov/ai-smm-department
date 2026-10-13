import { act, render } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect } from "react";
import { MemoryRouter, useLocation, useNavigate, type NavigateFunction } from "react-router";

import { ApiClient } from "../api/client";
import { createQueryClient } from "../api/context";
import { Providers } from "../App";
import { AppRoutes } from "../routes/AppRoutes";
import type { FakeApi } from "./fakeApi";

function LocationProbe() {
  const location = useLocation();

  return <div data-testid="location" hidden>{location.pathname + location.search}</div>;
}

let navigateRef: NavigateFunction | null = null;

/** Gives tests the router's navigate(), for URL changes and back / forward. */
function NavigateProbe() {
  const navigate = useNavigate();

  useEffect(() => {
    navigateRef = navigate;
  }, [navigate]);

  return null;
}

/** navigateTo("/path") pushes; navigateTo(-1) / navigateTo(1) go back / forward. */
export async function navigateTo(to: string | number) {
  await act(async () => {
    if (typeof to === "number") await navigateRef!(to);
    else await navigateRef!(to);
  });
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
        <NavigateProbe />
      </MemoryRouter>
    </Providers>,
  );

  return { ...utils, client, queryClient, user };
}

export const currentPath = () =>
  document.querySelector('[data-testid="location"]')?.textContent ?? "";
