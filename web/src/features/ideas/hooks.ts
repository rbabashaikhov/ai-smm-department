import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef } from "react";

import { useApi } from "../../api/context";
import { ApiError, ErrorCode } from "../../api/errors";
import { qk } from "../../api/queryKeys";
import type { ContentIdeaFilters, ContentIdeaUseResult, PageParams } from "../../types/api";

/** A failed POST may have reached the API. Refresh the server state before another command. */
export const isIdeaOutcomeUnknown = (error: unknown): error is ApiError =>
  error instanceof ApiError &&
  error.status === 0 &&
  error.code === ErrorCode.NETWORK_ERROR;

export function useContentIdeas(
  projectId: string | undefined,
  filters: ContentIdeaFilters,
  page: PageParams,
) {
  const api = useApi();

  return useQuery({
    queryKey: qk.contentIdeaList(projectId ?? "", filters, page),
    queryFn: () => api.ideas.list(projectId!, filters, page),
    enabled: Boolean(projectId),
  });
}

function useSingleIdeaCommand<T>(
  projectId: string,
  command: (ideaId: string) => Promise<T>,
  onSuccess: (value: T) => void,
) {
  const queryClient = useQueryClient();
  const inFlight = useRef<Promise<T> | null>(null);

  return useMutation({
    retry: false,
    mutationFn: (ideaId: string) => {
      if (inFlight.current) return inFlight.current;

      const request = command(ideaId);
      const clear = () => {
        if (inFlight.current === request) inFlight.current = null;
      };
      inFlight.current = request;
      void request.then(clear, clear);
      return request;
    },
    onSuccess: async (value) => {
      onSuccess(value);
      await queryClient.invalidateQueries({ queryKey: qk.contentIdeas(projectId) });
      await queryClient.invalidateQueries({ queryKey: qk.generationJobs(projectId) });
    },
    onError: async (error) => {
      if (isIdeaOutcomeUnknown(error)) {
        // There is no single-idea read endpoint: refetch every active page for this project.
        await queryClient.refetchQueries({ queryKey: qk.contentIdeas(projectId) });
      }
    },
  });
}

export function useRejectContentIdea(projectId: string) {
  const api = useApi();

  return useSingleIdeaCommand(projectId, (ideaId) => api.ideas.reject(projectId, ideaId), () => {});
}

export function useUseContentIdea(projectId: string) {
  const api = useApi();
  const queryClient = useQueryClient();

  return useSingleIdeaCommand<ContentIdeaUseResult>(
    projectId,
    (ideaId) => api.ideas.use(projectId, ideaId),
    (result) => {
      queryClient.setQueryData(
        qk.generationJob(result.generation_job.project_id, result.generation_job.id),
        result.generation_job,
      );
    },
  );
}
