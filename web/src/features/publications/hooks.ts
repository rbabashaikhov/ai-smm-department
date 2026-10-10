import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useApi } from "../../api/context";
import { qk } from "../../api/queryKeys";
import type { CancelRequest, PublicationStatus, ScheduleRequest } from "../../types/api";

export type PublicationCommand = "schedule" | "reschedule" | "cancel";

/**
 * Which command buttons to offer for a status. A copy of the backend's
 * COMMAND_POLICY used only to avoid showing a button that is certain to
 * be refused; the backend re-checks under a row lock and answers 409
 * INVALID_STATE_TRANSITION when the row moved in the meantime.
 */
const OFFERED_FROM: Record<PublicationCommand, readonly PublicationStatus[]> = {
  schedule: ["approved", "failed"],
  reschedule: ["scheduled", "failed"],
  cancel: ["draft", "approved", "scheduled", "failed"],
};

export const offersCommand = (command: PublicationCommand, status: PublicationStatus) =>
  OFFERED_FROM[command].includes(status);

export function usePublication(publicationId: number, options: { fresh?: boolean } = {}) {
  const api = useApi();

  return useQuery({
    queryKey: qk.publication(publicationId),
    queryFn: () => api.publications.get(publicationId),
    enabled: Number.isFinite(publicationId),
    // A confirmation dialog always reads the row again before it lets
    // anyone commit to it.
    ...(options.fresh ? { refetchOnMount: "always" as const, staleTime: 0 } : {}),
  });
}

/** schedule / reschedule / cancel. Never optimistic, never retried. */
export function usePublicationCommand(publicationId: number, projectId: string) {
  const api = useApi();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (
      input:
        | { command: "schedule" | "reschedule"; body: ScheduleRequest }
        | { command: "cancel"; body: CancelRequest },
    ) => {
      switch (input.command) {
        case "schedule":
          return api.publications.schedule(publicationId, input.body);
        case "reschedule":
          return api.publications.reschedule(publicationId, input.body);
        case "cancel":
          return api.publications.cancel(publicationId, input.body);
      }
    },
    onSuccess: async (detail) => {
      queryClient.setQueryData(qk.publication(publicationId), detail);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: qk.publication(publicationId) }),
        queryClient.invalidateQueries({ queryKey: qk.projectPublications(projectId) }),
        queryClient.invalidateQueries({ queryKey: qk.operations(projectId) }),
      ]);
    },
  });
}
