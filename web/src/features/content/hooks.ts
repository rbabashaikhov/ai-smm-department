import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useApi } from "../../api/context";
import { qk } from "../../api/queryKeys";
import type {
  ContentItemSummary,
  RevisionCreate,
  RevisionDecision,
} from "../../types/api";

const HISTORY_PAGE = { limit: 200, offset: 0 };

export function useContentItem(itemId: string) {
  const api = useApi();

  return useQuery({ queryKey: qk.contentItem(itemId), queryFn: () => api.content.get(itemId) });
}

export function useRevisions(itemId: string) {
  const api = useApi();

  return useQuery({
    queryKey: qk.revisions(itemId),
    queryFn: () => api.content.revisions(itemId, HISTORY_PAGE),
  });
}

export function useApprovals(itemId: string) {
  const api = useApi();

  return useQuery({
    queryKey: qk.approvals(itemId),
    queryFn: () => api.content.approvals(itemId, HISTORY_PAGE),
  });
}

/**
 * The editorial commands. None is optimistic: the page shows what the
 * server answered, after it answered. Each success invalidates the item
 * (and with it its revisions and approvals) and the project's content
 * lists; materialize also the project's publications and counters.
 */
export function useContentCommands(itemId: string, projectId: string | undefined) {
  const api = useApi();
  const queryClient = useQueryClient();

  const refreshItem = async () => {
    await queryClient.invalidateQueries({ queryKey: qk.contentItem(itemId) });

    if (projectId) {
      await queryClient.invalidateQueries({ queryKey: qk.projectContent(projectId) });
    }
  };

  const createRevision = useMutation({
    mutationFn: (body: RevisionCreate) => api.content.createRevision(itemId, body),
    onSuccess: refreshItem,
  });

  const submitReview = useMutation({
    mutationFn: (body: RevisionDecision) => api.content.submitReview(itemId, body),
    onSuccess: refreshItem,
  });

  const approve = useMutation({
    mutationFn: (body: RevisionDecision) => api.content.approve(itemId, body),
    onSuccess: refreshItem,
  });

  const reject = useMutation({
    mutationFn: (body: RevisionDecision) => api.content.reject(itemId, body),
    onSuccess: refreshItem,
  });

  const materialize = useMutation({
    mutationFn: (revisionId: string) => api.content.materialize(itemId, revisionId),
    onSuccess: async (result) => {
      queryClient.setQueryData(qk.publication(result.publication.id), result.publication);
      await refreshItem();

      if (projectId) {
        await Promise.all([
          queryClient.invalidateQueries({ queryKey: qk.projectPublications(projectId) }),
          queryClient.invalidateQueries({ queryKey: qk.operations(projectId) }),
        ]);
      }
    },
  });

  return { createRevision, submitReview, approve, reject, materialize };
}

export type ItemSnapshot = Pick<ContentItemSummary, "id" | "version" | "status">;
