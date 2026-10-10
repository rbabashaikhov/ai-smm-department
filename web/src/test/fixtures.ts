import type {
  Approval,
  ContentItemDetail,
  ContentStatus,
  MeResponse,
  OperationsSummary,
  Project,
  PublicationDetail,
  PublicationPreview,
  PublicationStatus,
  Revision,
  Role,
} from "../types/api";
import { FakeApi, ok } from "./fakeApi";

export const PROJECT_ID = "demo";
export const ITEM_ID = "11111111-1111-4111-8111-111111111111";
export const REV1_ID = "22222222-2222-4222-8222-222222222221";
export const REV2_ID = "22222222-2222-4222-8222-222222222222";
export const USER_ID = "33333333-3333-4333-8333-333333333333";
export const PUBLICATION_ID = 42;

const T0 = "2026-10-10T09:00:00+00:00";

export const page = <T,>(items: T[], total = items.length, limit = 50, offset = 0) => ({
  items,
  total,
  limit,
  offset,
});

export function makeMe(role: Role, mutationsEnabled = true): MeResponse {
  return {
    user: {
      id: USER_ID,
      email: "operator@example.com",
      display_name: "Operator",
      is_active: true,
      last_login_at: T0,
      created_at: T0,
    },
    projects: [{ project_id: PROJECT_ID, display_name: "Demo Project", role }],
    control_plane: { mutations_enabled: mutationsEnabled },
  };
}

export function makeProject(role: Role): Project {
  return {
    id: PROJECT_ID,
    display_name: "Demo Project",
    description: "Test project",
    default_platform: "threads",
    default_timezone: "Europe/Moscow",
    default_language: "ru",
    is_active: true,
    version: 1,
    created_at: T0,
    updated_at: T0,
    role,
  };
}

export function makeRevision(overrides: Partial<Revision> = {}): Revision {
  return {
    id: REV1_ID,
    content_item_id: ITEM_ID,
    revision_number: 1,
    title: "Revision title",
    body: "Exact body of revision one",
    format: "text",
    images: [],
    items: [],
    metadata: {},
    source: "human",
    source_ref: "brief:7",
    editor_score: 8.5,
    editor_notes: "Tight copy",
    content_hash: "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
    created_by_user_id: USER_ID,
    created_at: T0,
    ...overrides,
  };
}

export function makeItem(
  status: ContentStatus = "draft",
  overrides: Partial<ContentItemDetail> = {},
): ContentItemDetail {
  const revision = overrides.current_revision ?? makeRevision();
  const approved = status === "approved";

  return {
    id: ITEM_ID,
    project_id: PROJECT_ID,
    title: "Launch post",
    content_type: "post",
    status,
    current_revision_id: revision.id,
    approved_revision_id: approved ? revision.id : null,
    version: 3,
    created_by_user_id: USER_ID,
    created_at: T0,
    updated_at: T0,
    archived_at: null,
    current_revision: revision,
    approval_in_effect: approved,
    ...overrides,
  };
}

export function makeApproval(overrides: Partial<Approval> = {}): Approval {
  return {
    id: "44444444-4444-4444-8444-444444444444",
    content_item_id: ITEM_ID,
    revision_id: REV1_ID,
    decision: "approved",
    actor_user_id: USER_ID,
    note: "",
    created_at: T0,
    ...overrides,
  };
}

export function makePublication(
  status: PublicationStatus = "approved",
  overrides: Partial<PublicationDetail> = {},
): PublicationDetail {
  return {
    id: PUBLICATION_ID,
    project_id: PROJECT_ID,
    platform: "threads",
    ordinal: 7,
    title: "Snapshot title",
    format: "text",
    status,
    scheduled_at: status === "scheduled" ? "2026-10-12T06:00:00+00:00" : null,
    published_at: null,
    threads_post_id: null,
    attempt_count: 0,
    human_reviewed: true,
    series_id: null,
    series_position: null,
    series_total: null,
    image_count: 0,
    body_chars: 26,
    created_at: T0,
    updated_at: T0,
    body: "Exact immutable snapshot body",
    images: [],
    items: [],
    editor_score: 8.5,
    last_error: null,
    next_attempt_at: null,
    claimed_by: null,
    claimed_at: null,
    lease_expires_at: null,
    parent_publication_id: null,
    previous_publication_id: null,
    source_ref: `content_item:${ITEM_ID}`,
    idempotency_key: `content:${ITEM_ID}:${REV1_ID}:threads`,
    ...overrides,
  };
}

export function makeSummary(overrides: Partial<OperationsSummary> = {}): OperationsSummary {
  return {
    project_id: PROJECT_ID,
    total: 12,
    by_status: {
      draft: 0,
      approved: 2,
      scheduled: 3,
      claimed: 0,
      publishing: 0,
      published: 5,
      failed: 1,
      needs_review: 1,
      cancelled: 0,
    },
    due_now: 1,
    needs_attention: 2,
    published_last_24h: 4,
    last_published_at: "2026-10-10T08:00:00+00:00",
    next_scheduled_at: "2026-10-12T06:00:00+00:00",
    series_total: 2,
    series_active: 1,
    api_dry_run: false,
    api_mutations_enabled: true,
    worker_mode: "unknown",
    worker_mode_source: null,
    ...overrides,
  };
}

export function makePreview(detail: PublicationDetail): PublicationPreview {
  return {
    publication: detail,
    content_valid: true,
    content_error: null,
    human_reviewed: detail.human_reviewed,
    series_ready: true,
    series_reason: null,
    blocking_parts: [],
    publishable_now: true,
  };
}

/**
 * A fake backend with a signed-in user of `role` (or no session when
 * `role` is null) and one of everything.
 */
export function createServer(
  role: Role | null,
  options: {
    item?: ContentItemDetail;
    publication?: PublicationDetail;
    /** false: a read-only deployment (AI_SMM_API_MUTATIONS_ENABLED=false). */
    mutationsEnabled?: boolean;
  } = {},
): FakeApi {
  const api = new FakeApi();
  const item = options.item ?? makeItem();
  const publication = options.publication ?? makePublication();
  let signedIn = role !== null;
  const activeRole: Role = role ?? "viewer";
  const mutationsEnabled = options.mutationsEnabled ?? true;
  const unauthorized = {
    status: 401,
    body: { error: { code: "AUTH_REQUIRED", message: "Authentication is required.", details: {}, request_id: "r" } },
  };

  api
    .on("GET", "/api/v1/auth/me", () =>
      signedIn ? ok(makeMe(activeRole, mutationsEnabled)) : unauthorized,
    )
    .on("GET", "/api/v1/auth/csrf", () =>
      signedIn ? ok({ csrf_token: api.csrfToken, header_name: "X-CSRF-Token" }) : unauthorized,
    )
    .on("POST", "/api/v1/auth/login", () => {
      signedIn = true;

      return ok({ user: makeMe(activeRole, mutationsEnabled).user, csrf_token: api.csrfToken });
    })
    .on("POST", "/api/v1/auth/logout", () => {
      signedIn = false;

      return { status: 204 };
    })
    .on("GET", "/api/v1/projects", ok([makeProject(activeRole)]))
    .on("GET", "/api/v1/projects/:projectId", ok(makeProject(activeRole)))
    .on(
      "GET",
      "/api/v1/projects/:projectId/settings",
      ok({
        project_id: PROJECT_ID,
        content_config: { tone: "dry" },
        publishing_config: {},
        brand_config: {},
        version: 2,
        updated_at: T0,
      }),
    )
    .on(
      "GET",
      "/api/v1/projects/:projectId/operations/summary",
      ok(makeSummary({ api_mutations_enabled: mutationsEnabled })),
    )
    .on("GET", "/api/v1/projects/:projectId/operations/attention", ok(page([])))
    .on("GET", "/api/v1/projects/:projectId/audit", ok(page([])))
    .on("GET", "/api/v1/projects/:projectId/content-items", ok(page([item])))
    .on("GET", "/api/v1/projects/:projectId/publications", ok(page([publication])))
    .on("GET", "/api/v1/content-items/:id", ok(item))
    .on("GET", "/api/v1/content-items/:id/revisions", ok(page([item.current_revision])))
    .on("GET", "/api/v1/content-items/:id/approvals", ok(page([])))
    .on("GET", "/api/v1/publications/:id", ok(publication))
    .on("GET", "/api/v1/publications/:id/preview", ok(makePreview(publication)))
    .on("GET", "/api/v1/publications/:id/attempts", ok(page([])));

  return api;
}
