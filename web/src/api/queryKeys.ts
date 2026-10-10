// Query keys are hierarchical so a mutation can invalidate exactly the
// subtree it affected: ["projects", id, "content"] covers every content
// list of one project and nothing else.

import type { ContentFilters, PageParams, PublicationFilters } from "../types/api";

export const qk = {
  projects: () => ["projects"] as const,
  project: (projectId: string) => ["projects", projectId] as const,
  settings: (projectId: string) => ["projects", projectId, "settings"] as const,

  operations: (projectId: string) => ["projects", projectId, "operations"] as const,
  operationsSummary: (projectId: string) =>
    ["projects", projectId, "operations", "summary"] as const,
  attention: (projectId: string, page: PageParams) =>
    ["projects", projectId, "operations", "attention", page] as const,
  audit: (projectId: string, page: PageParams, action: string) =>
    ["projects", projectId, "audit", { ...page, action }] as const,

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
  revisions: (itemId: string) => ["content-items", itemId, "revisions"] as const,
  approvals: (itemId: string) => ["content-items", itemId, "approvals"] as const,

  publication: (publicationId: number) => ["publications", publicationId] as const,
  preview: (publicationId: number) => ["publications", publicationId, "preview"] as const,
  attempts: (publicationId: number) => ["publications", publicationId, "attempts"] as const,
};
