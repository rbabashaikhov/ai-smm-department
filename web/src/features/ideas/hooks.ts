import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useRef, useState } from "react";

import { useApi } from "../../api/context";
import { ApiError, ErrorCode } from "../../api/errors";
import { qk } from "../../api/queryKeys";
import type {
  ContentIdeaFilters,
  ContentIdeaUseResult,
  PageParams,
  TopicIdeationJob,
  TopicIdeationJobCreate,
  TopicIdeationJobFilters,
} from "../../types/api";

export const TOPIC_IDEATION_POLL_INTERVAL_MS = 3_000;

function topicIdeationPollingInterval(jobs: TopicIdeationJob[] | undefined): number | false {
  return jobs?.some((job) => job.status === "queued" || job.status === "claimed")
    ? TOPIC_IDEATION_POLL_INTERVAL_MS
    : false;
}

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

export function useTopicIdeationJobs(
  projectId: string | undefined,
  filters: TopicIdeationJobFilters,
  page: PageParams,
) {
  const api = useApi();

  return useQuery({
    queryKey: qk.topicIdeationJobList(projectId ?? "", filters, page),
    queryFn: () => api.ideas.listJobs(projectId!, filters, page),
    enabled: Boolean(projectId),
    refetchInterval: (query) => topicIdeationPollingInterval(query.state.data?.items),
  });
}

/** A lost create response is never retried; the owner explicitly rereads jobs first. */
export function useCreateTopicIdeationJob(projectId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  const inFlight = useRef<Promise<TopicIdeationJob> | null>(null);

  return useMutation({
    retry: false,
    mutationFn: (body: TopicIdeationJobCreate) => {
      if (inFlight.current) return inFlight.current;
      const request = api.ideas.createJob(projectId, body);
      const clear = () => {
        if (inFlight.current === request) inFlight.current = null;
      };
      inFlight.current = request;
      void request.then(clear, clear);
      return request;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: qk.topicIdeationJobs(projectId) });
    },
  });
}

function useSingleIdeaCommand<T>(
  projectId: string,
  command: (ideaId: string) => Promise<T>,
  onSuccess: (value: T) => void,
) {
  const queryClient = useQueryClient();
  // Keep a successful command by idea id for the lifetime of this page.
  // A completed idea no longer exposes an action after the list refreshes;
  // retaining its promise also closes the narrow double-click window before
  // that render happens.
  const commands = useRef(new Map<string, Promise<T>>());

  return useMutation({
    retry: false,
    mutationFn: (ideaId: string) => {
      const existing = commands.current.get(ideaId);
      if (existing) return existing;

      const request = command(ideaId);
      commands.current.set(ideaId, request);
      return request;
    },
    onSuccess: async (value) => {
      onSuccess(value);
      await queryClient.invalidateQueries({ queryKey: qk.contentIdeas(projectId) });
      await queryClient.invalidateQueries({ queryKey: qk.generationJobs(projectId) });
    },
    onError: (_error, ideaId) => {
      commands.current.delete(ideaId);
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
  const commands = useRef(new Map<string, Promise<ContentIdeaUseResult>>());
  const [verification, setVerification] = useState<Record<string, "checking" | "failed">>({});

  const rereadIdeaState = useCallback(async (ideaId: string) => {
    setVerification((current) => ({ ...current, [ideaId]: "checking" }));

    try {
      // Only a successful server read permits another command. throwOnError
      // matters here: refetchQueries otherwise resolves even when its query
      // failed, which would re-offer an action from stale cache data.
      await queryClient.refetchQueries(
        { queryKey: qk.contentIdeas(projectId), type: "active" },
        { throwOnError: true },
      );
      commands.current.delete(ideaId);
      setVerification((current) => {
        const { [ideaId]: _verified, ...rest } = current;
        return rest;
      });
      return true;
    } catch {
      setVerification((current) => ({ ...current, [ideaId]: "failed" }));
      return false;
    }
  }, [projectId, queryClient]);

  const mutation = useMutation({
    retry: false,
    mutationFn: (ideaId: string) => {
      const existing = commands.current.get(ideaId);
      if (existing) return existing;

      const request = api.ideas.use(projectId, ideaId);
      commands.current.set(ideaId, request);
      return request;
    },
    onSuccess: async (result) => {
      queryClient.setQueryData(
        qk.generationJob(result.generation_job.project_id, result.generation_job.id),
        result.generation_job,
      );
      await queryClient.invalidateQueries({ queryKey: qk.contentIdeas(projectId) });
      await queryClient.invalidateQueries({ queryKey: qk.generationJobs(projectId) });
    },
    onError: async (error, ideaId) => {
      if (isIdeaOutcomeUnknown(error)) {
        await rereadIdeaState(ideaId);
        return;
      }
      commands.current.delete(ideaId);
    },
  });

  return {
    ...mutation,
    isIdeaBlocked: (ideaId: string) => verification[ideaId] !== undefined,
    isIdeaVerifying: (ideaId: string) => verification[ideaId] === "checking",
    isIdeaBusy: (ideaId: string) => mutation.isPending && mutation.variables === ideaId,
    verifyIdeaState: rereadIdeaState,
  };
}
