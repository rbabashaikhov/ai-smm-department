// Typed wrappers, one per backend route. Components and hooks call these;
// none of them builds a URL by hand.
//
// There is deliberately no function that publishes. The control plane can
// schedule, reschedule and cancel a publication; only the worker sends one.

import type { ApiClient } from "./client";
import type {
  Approval,
  Attempt,
  AuditEntry,
  CancelRequest,
  ContentFilters,
  ContentItemCreate,
  ContentItemDetail,
  ContentItemSummary,
  CsrfResponse,
  DecisionResult,
  LoginResponse,
  MaterializeResult,
  MeResponse,
  OperationsSummary,
  Page,
  PageParams,
  Project,
  ProjectSettings,
  PublicationDetail,
  PublicationFilters,
  PublicationPreview,
  PublicationSummary,
  Revision,
  RevisionCreate,
  RevisionCreated,
  RevisionDecision,
  ScheduleRequest,
} from "../types/api";

const V1 = "/api/v1";

const enc = encodeURIComponent;

export function createEndpoints(client: ApiClient) {
  return {
    auth: {
      login: (email: string, password: string) =>
        client.post<LoginResponse>(`${V1}/auth/login`, { email, password }),
      logout: () => client.post<void>(`${V1}/auth/logout`),
      me: () => client.get<MeResponse>(`${V1}/auth/me`),
      csrf: () => client.get<CsrfResponse>(`${V1}/auth/csrf`),
    },

    projects: {
      list: () => client.get<Project[]>(`${V1}/projects`),
      get: (projectId: string) => client.get<Project>(`${V1}/projects/${enc(projectId)}`),
      settings: (projectId: string) =>
        client.get<ProjectSettings>(`${V1}/projects/${enc(projectId)}/settings`),
      operationsSummary: (projectId: string) =>
        client.get<OperationsSummary>(
          `${V1}/projects/${enc(projectId)}/operations/summary`,
        ),
      attention: (projectId: string, page: PageParams) =>
        client.get<Page<PublicationSummary>>(
          `${V1}/projects/${enc(projectId)}/operations/attention`,
          { query: { ...page } },
        ),
      audit: (projectId: string, page: PageParams, action?: string) =>
        client.get<Page<AuditEntry>>(`${V1}/projects/${enc(projectId)}/audit`, {
          query: { ...page, action },
        }),
    },

    publications: {
      list: (projectId: string, filters: PublicationFilters, page: PageParams) =>
        client.get<Page<PublicationSummary>>(
          `${V1}/projects/${enc(projectId)}/publications`,
          {
            query: {
              ...page,
              status: filters.status,
              format: filters.format,
              series_id: filters.series_id,
              human_reviewed: filters.human_reviewed,
            },
          },
        ),
      get: (publicationId: number) =>
        client.get<PublicationDetail>(`${V1}/publications/${publicationId}`),
      preview: (publicationId: number) =>
        client.get<PublicationPreview>(`${V1}/publications/${publicationId}/preview`),
      attempts: (publicationId: number, page: PageParams) =>
        client.get<Page<Attempt>>(`${V1}/publications/${publicationId}/attempts`, {
          query: { ...page },
        }),
      schedule: (publicationId: number, body: ScheduleRequest) =>
        client.post<PublicationDetail>(
          `${V1}/publications/${publicationId}/schedule`,
          body,
        ),
      reschedule: (publicationId: number, body: ScheduleRequest) =>
        client.post<PublicationDetail>(
          `${V1}/publications/${publicationId}/reschedule`,
          body,
        ),
      cancel: (publicationId: number, body: CancelRequest) =>
        client.post<PublicationDetail>(`${V1}/publications/${publicationId}/cancel`, body),
    },

    content: {
      list: (projectId: string, filters: ContentFilters, page: PageParams) =>
        client.get<Page<ContentItemSummary>>(
          `${V1}/projects/${enc(projectId)}/content-items`,
          {
            query: {
              ...page,
              status: filters.status,
              content_type: filters.content_type,
            },
          },
        ),
      create: (projectId: string, body: ContentItemCreate) =>
        client.post<ContentItemDetail>(
          `${V1}/projects/${enc(projectId)}/content-items`,
          body,
        ),
      get: (itemId: string) =>
        client.get<ContentItemDetail>(`${V1}/content-items/${enc(itemId)}`),
      revisions: (itemId: string, page: PageParams) =>
        client.get<Page<Revision>>(`${V1}/content-items/${enc(itemId)}/revisions`, {
          query: { ...page },
        }),
      approvals: (itemId: string, page: PageParams) =>
        client.get<Page<Approval>>(`${V1}/content-items/${enc(itemId)}/approvals`, {
          query: { ...page },
        }),
      /** Never a PATCH: a change is always a new, immutable revision. */
      createRevision: (itemId: string, body: RevisionCreate) =>
        client.post<RevisionCreated>(`${V1}/content-items/${enc(itemId)}/revisions`, body),
      submitReview: (itemId: string, body: RevisionDecision) =>
        client.post<ContentItemSummary>(
          `${V1}/content-items/${enc(itemId)}/submit-review`,
          body,
        ),
      approve: (itemId: string, body: RevisionDecision) =>
        client.post<DecisionResult>(`${V1}/content-items/${enc(itemId)}/approve`, body),
      reject: (itemId: string, body: RevisionDecision) =>
        client.post<DecisionResult>(`${V1}/content-items/${enc(itemId)}/reject`, body),
      /** Creates a delivery snapshot in `approved`. It does not publish. */
      materialize: (itemId: string, revisionId: string) =>
        client.post<MaterializeResult>(`${V1}/content-items/${enc(itemId)}/materialize`, {
          revision_id: revisionId,
        }),
    },
  };
}

export type Endpoints = ReturnType<typeof createEndpoints>;
