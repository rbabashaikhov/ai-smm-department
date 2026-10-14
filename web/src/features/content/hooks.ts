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
 * What smm-auth says approving `revisionId` would publish, read for the
 * confirmation dialog only: fetched when it opens, never cached, never
 * retried (a 404 means "not yours, or gone").
 */
export function useApprovalPreview(revisionId: string | null) {
  const api = useApi();

  return useQuery({
    queryKey: qk.approvalPreview(revisionId ?? ""),
    queryFn: () => api.auth.approvalPreview(revisionId ?? ""),
    enabled: revisionId !== null,
    staleTime: 0,
    gcTime: 0,
    retry: false,
  });
}

/** A password that never prints itself: not in devtools, logs or errors. */
export interface StepUpPassword {
  reveal: () => string;
  toString: () => string;
  toJSON: () => string;
}

export const stepUpPassword = (value: string): StepUpPassword =>
  Object.freeze({
    reveal: () => value,
    toString: () => "[password]",
    toJSON: () => "[password]",
  });

export type ApproveInput = RevisionDecision & { password: StepUpPassword };

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

  // Two requests, in this order (ADR-025D §4.3): smm-auth re-checks the
  // password and issues a grant for exactly this revision; the approval
  // then consumes it in the database. No grant, no approval -- whatever
  // this code does. gcTime 0: the password does not outlive the attempt.
  const approve = useMutation({
    gcTime: 0,
    mutationFn: async ({ password, ...body }: ApproveInput) => {
      await api.auth.stepUpApproval(body.revision_id, password.reveal());

      return api.content.approve(itemId, body);
    },
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
