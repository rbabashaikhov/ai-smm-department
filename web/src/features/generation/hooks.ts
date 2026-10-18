import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef } from "react";

import { useApi } from "../../api/context";
import { ApiError, ErrorCode } from "../../api/errors";
import { qk } from "../../api/queryKeys";
import type {
  GenerationJob,
  GenerationJobCreate,
  GenerationJobFilters,
  PageParams,
} from "../../types/api";

export const GENERATION_POLL_INTERVAL_MS = 5_000;

export function generationPollingInterval(
  job: GenerationJob | undefined,
  hasError = false,
): number | false {
  if (hasError || !job) return false;

  return job.status === "queued" || job.status === "claimed"
    ? GENERATION_POLL_INTERVAL_MS
    : false;
}

/** A failed POST may have reached the server; the list is the safe recovery path. */
export const isGenerationOutcomeUnknown = (error: unknown): error is ApiError =>
  error instanceof ApiError &&
  error.status === 0 &&
  error.code === ErrorCode.NETWORK_ERROR;

export function useGenerationJobs(
  projectId: string | undefined,
  filters: GenerationJobFilters,
  page: PageParams,
) {
  const api = useApi();

  return useQuery({
    queryKey: qk.generationJobList(projectId ?? "", filters, page),
    queryFn: () => api.generation.list(projectId!, filters, page),
    enabled: Boolean(projectId),
  });
}

export function useGenerationJob(
  projectId: string | undefined,
  jobId: string | undefined,
) {
  const api = useApi();

  return useQuery({
    queryKey: qk.generationJob(projectId ?? "", jobId ?? ""),
    queryFn: () => api.generation.get(jobId!),
    enabled: Boolean(projectId && jobId),
    refetchInterval: (query) =>
      generationPollingInterval(query.state.data, query.state.status === "error"),
  });
}

export function useCreateGenerationJob(projectId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  const inFlight = useRef<Promise<GenerationJob> | null>(null);

  return useMutation({
    retry: false,
    mutationFn: (body: GenerationJobCreate) => {
      if (inFlight.current) return inFlight.current;

      const request = api.generation.create(projectId, body);
      const clear = () => {
        if (inFlight.current === request) inFlight.current = null;
      };

      inFlight.current = request;
      void request.then(clear, clear);

      return request;
    },
    onSuccess: async (job) => {
      queryClient.setQueryData(qk.generationJob(job.project_id, job.id), job);
      await queryClient.invalidateQueries({ queryKey: qk.generationJobs(projectId) });
    },
  });
}

export function useCancelGenerationJob(projectId: string, jobId: string) {
  const api = useApi();
  const queryClient = useQueryClient();

  return useMutation({
    retry: false,
    mutationFn: () => api.generation.cancel(jobId),
    onSuccess: async (job) => {
      queryClient.setQueryData(qk.generationJob(job.project_id, job.id), job);
      await queryClient.invalidateQueries({ queryKey: qk.generationJobs(projectId) });
    },
  });
}
