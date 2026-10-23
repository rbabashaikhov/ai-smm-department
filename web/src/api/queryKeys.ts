// Query keys are hierarchical so a mutation can invalidate exactly the
// subtree it affected: ["projects", id, "content"] covers every content
// list of one project and nothing else.

import type {
  ContentFilters,
  ContentIdeaFilters,
  GenerationJobFilters,
  PageParams,
  PublicationFilters,
  TopicIdeationJobFilters,
} from "../types/api";

export const qk = {
  projects: () => ["projects"] as const,
  project: (projectId: string) => ["projects", projectId] as const,
  settings: (projectId: string) => ["projects", projectId, "settings"] as const,
  knowledge: (projectId: string) => ["projects", projectId, "knowledge"] as const,
  knowledgeDocument: (projectId: string, name: string) =>
    ["projects", projectId, "knowledge", name] as const,
  accounts: (projectId: string) => ["projects", projectId, "accounts"] as const,
  account: (accountId: string) => ["social-accounts", accountId] as const,

  operations: (projectId: string) => ["projects", projectId, "operations"] as const,
  operationsSummary: (projectId: string) =>
    ["projects", projectId, "operations", "summary"] as const,
  attention: (projectId: string, page: PageParams) =>
    ["projects", projectId, "operations", "attention", page] as const,
  audit: (projectId: string, page: PageParams, action: string) =>
    ["projects", projectId, "audit", { ...page, action }] as const,

  generationJobs: (projectId: string) =>
    ["projects", projectId, "generation-jobs"] as const,
  generationJobList: (
    projectId: string,
    filters: GenerationJobFilters,
    page: PageParams,
  ) => ["projects", projectId, "generation-jobs", "list", { ...filters, ...page }] as const,
  generationJob: (projectId: string, jobId: string) =>
    ["projects", projectId, "generation-jobs", jobId] as const,

  contentIdeas: (projectId: string) => ["projects", projectId, "content-ideas"] as const,
  contentIdeaList: (projectId: string, filters: ContentIdeaFilters, page: PageParams) =>
    ["projects", projectId, "content-ideas", "list", { ...filters, ...page }] as const,
  topicIdeationJobs: (projectId: string) =>
    ["projects", projectId, "topic-ideation-jobs"] as const,
  topicIdeationJobList: (
    projectId: string,
    filters: TopicIdeationJobFilters,
    page: PageParams,
  ) => ["projects", projectId, "topic-ideation-jobs", "list", { ...filters, ...page }] as const,

  projectContent: (projectId: string) => ["projects", projectId, "content"] as const,
  contentList: (projectId: string, filters: ContentFilters, page: PageParams) =>
    ["projects", projectId, "content", "list", { ...filters, ...page }] as const,
  contentStatusTotals: (projectId: string) =>
    ["projects", projectId, "content", "status-totals"] as const,

  projectPublications: (projectId: string) =>
    ["projects", projectId, "publications"] as const,
  publicationList: (projectId: string, filters: PublicationFilters, page: PageParams) =>
    ["projects", projectId, "publications", { ...filters, ...page }] as const,

  contentItem: (itemId: string) => ["content-items", itemId] as const,
  /** smm-auth's preview of one approval; never cached (gcTime 0). */
  approvalPreview: (revisionId: string) => ["step-up", "approvals", revisionId] as const,
  revisions: (itemId: string) => ["content-items", itemId, "revisions"] as const,
  approvals: (itemId: string) => ["content-items", itemId, "approvals"] as const,

  publication: (publicationId: number) => ["publications", publicationId] as const,
  preview: (publicationId: number) => ["publications", publicationId, "preview"] as const,
  attempts: (publicationId: number) => ["publications", publicationId, "attempts"] as const,
};
