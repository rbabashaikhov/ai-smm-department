// Typed wrappers, one per backend route. Components and hooks call these;
// none of them builds a URL by hand.
//
// There is deliberately no function that publishes. The control plane can
// schedule, reschedule and cancel a publication; only the worker sends one.

import type { ApiClient } from "./client";
import type {
  Approval,
  ApprovalPreview,
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
  Credential,
  CredentialInput,
  GenerationJob,
  GenerationJobCreate,
  GenerationJobFilters,
  GenerationJobList,
  Project,
  ProjectCreate,
  ProjectPatch,
  ProjectSettings,
  ProjectSettingsPatch,
  PublicationDetail,
  PublicationFilters,
  PublicationPreview,
  PublicationSummary,
  Revision,
  RevisionCreate,
  RevisionCreated,
  RevisionDecision,
  ScheduleRequest,
  SocialAccount,
  SocialAccountDetail,
  StepUpGrant,
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
      /** smm-auth: what approving the revision would publish (owner only). */
      approvalPreview: (revisionId: string) =>
        client.get<ApprovalPreview>(`${V1}/auth/step-up/approvals/${enc(revisionId)}`),
      /** smm-auth: re-checks the password, issues the grant the approval consumes. */
      stepUpApproval: (revisionId: string, password: string) =>
        client.post<StepUpGrant>(`${V1}/auth/step-up/approvals`, {
          revision_id: revisionId,
          password,
        }),
    },

    projects: {
      list: () => client.get<Project[]>(`${V1}/projects`),
      get: (projectId: string) => client.get<Project>(`${V1}/projects/${enc(projectId)}`),
      /** The caller becomes the owner. Same id + same name again -> 200, same project. */
      create: (body: ProjectCreate) => client.post<Project>(`${V1}/projects`, body),
      update: (projectId: string, body: ProjectPatch) =>
        client.patch<Project>(`${V1}/projects/${enc(projectId)}`, body),
      settings: (projectId: string) =>
        client.get<ProjectSettings>(`${V1}/projects/${enc(projectId)}/settings`),
      updateSettings: (projectId: string, body: ProjectSettingsPatch) =>
        client.patch<ProjectSettings>(`${V1}/projects/${enc(projectId)}/settings`, body),
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

    generation: {
      create: (projectId: string, body: GenerationJobCreate) =>
        client.post<GenerationJob>(
          `${V1}/projects/${enc(projectId)}/generation-jobs`,
          body,
        ),
      list: (projectId: string, filters: GenerationJobFilters, page: PageParams) =>
        client.get<GenerationJobList>(
          `${V1}/projects/${enc(projectId)}/generation-jobs`,
          { query: { ...page, status: filters.status } },
        ),
      get: (jobId: string) =>
        client.get<GenerationJob>(`${V1}/generation-jobs/${enc(jobId)}`),
      cancel: (jobId: string) =>
        client.post<GenerationJob>(`${V1}/generation-jobs/${enc(jobId)}/cancel`),
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

    accounts: {
      list: (projectId: string) =>
        client.get<SocialAccount[]>(`${V1}/projects/${enc(projectId)}/social-accounts`),
      get: (accountId: string) =>
        client.get<SocialAccountDetail>(`${V1}/social-accounts/${enc(accountId)}`),
      create: (projectId: string, displayName: string) =>
        client.post<SocialAccount>(`${V1}/projects/${enc(projectId)}/social-accounts`, {
          platform: "threads",
          display_name: displayName,
        }),
      rename: (accountId: string, expectedVersion: number, displayName: string) =>
        client.patch<SocialAccount>(`${V1}/social-accounts/${enc(accountId)}`, {
          expected_version: expectedVersion,
          display_name: displayName,
        }),
      disable: (accountId: string, expectedVersion: number) =>
        client.post<SocialAccount>(`${V1}/social-accounts/${enc(accountId)}/disable`, {
          expected_version: expectedVersion,
        }),
      enable: (accountId: string, expectedVersion: number) =>
        client.post<SocialAccount>(`${V1}/social-accounts/${enc(accountId)}/enable`, {
          expected_version: expectedVersion,
        }),
      /** Write-only: the response is metadata, the token never comes back. */
      setCredential: (accountId: string, body: CredentialInput) =>
        client.post<Credential>(`${V1}/social-accounts/${enc(accountId)}/credentials`, body),
      revokeCredential: (credentialId: string) =>
        client.post<Credential>(`${V1}/social-account-credentials/${enc(credentialId)}/revoke`),
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
