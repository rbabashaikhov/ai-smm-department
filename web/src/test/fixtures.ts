import type {
  Approval,
  ApprovalPreview,
  ContentItemDetail,
  ContentStatus,
  Credential,
  MeResponse,
  OperationsSummary,
  Project,
  PublicationDetail,
  PublicationPreview,
  PublicationStatus,
  Revision,
  Role,
  SocialAccount,
  TopicIdeationJob,
} from "../types/api";
import { FakeApi, ok } from "./fakeApi";

export const PROJECT_ID = "demo";
export const ITEM_ID = "11111111-1111-4111-8111-111111111111";
export const REV1_ID = "22222222-2222-4222-8222-222222222221";
export const REV2_ID = "22222222-2222-4222-8222-222222222222";
export const USER_ID = "33333333-3333-4333-8333-333333333333";
export const PUBLICATION_ID = 42;

export function makeTopicIdeationJob(
  overrides: Partial<TopicIdeationJob> = {},
): TopicIdeationJob {
  return {
    id: "99999999-9999-4999-8999-999999999999",
    project_id: PROJECT_ID,
    requested_by_user_id: USER_ID,
    status: "queued",
    idea_count: 3,
    topic_hint: null,
    result: {},
    last_error: null,
    attempt_count: 0,
    max_attempts: 2,
    created_at: T0,
    updated_at: T0,
    finished_at: null,
    ...overrides,
  };
}

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

/** smm-auth's reading of an item's current revision (step-up preview). */
export function makeApprovalPreview(
  item: ContentItemDetail,
  overrides: Partial<ApprovalPreview> = {},
): ApprovalPreview {
  const revision = item.current_revision;

  return {
    content_item_id: item.id,
    project_id: item.project_id,
    revision_id: revision.id,
    revision_number: revision.revision_number,
    is_current: true,
    item_status: item.status,
    title: revision.title ?? item.title,
    body: revision.body,
    format: revision.format,
    images: revision.images,
    items: revision.items,
    content_hash: revision.content_hash,
    social_account_id: revision.social_account_id ?? null,
    account_display_name: null,
    account_external_username: null,
    account_ready: null,
    ...overrides,
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
    .on("GET", "/api/v1/projects/:projectId/topic-ideation-jobs", ok(page([])))
    .on("GET", "/api/v1/projects/:projectId/audit", ok(page([])))
    .on("GET", "/api/v1/projects/:projectId/content-items", ok(page([item])))
    .on("GET", "/api/v1/projects/:projectId/publications", ok(page([publication])))
    .on("GET", "/api/v1/content-items/:id", ok(item))
    .on("GET", "/api/v1/content-items/:id/revisions", ok(page([item.current_revision])))
    .on("GET", "/api/v1/content-items/:id/approvals", ok(page([])))
    .on("GET", "/api/v1/publications/:id", ok(publication))
    .on("GET", "/api/v1/publications/:id/preview", ok(makePreview(publication)))
    .on("GET", "/api/v1/publications/:id/attempts", ok(page([])))
    .on("GET", "/api/v1/projects/:projectId/social-accounts", ok([]))
    // smm-auth's step-up (SMM-025D), behind the same origin.
    .on("GET", "/api/v1/auth/step-up/approvals/:revisionId", ok(makeApprovalPreview(item)))
    .on("POST", "/api/v1/auth/step-up/approvals", (req) =>
      ok(
        {
          grant_id: "88888888-8888-4888-8888-888888888888",
          revision_id: (req.body as { revision_id: string }).revision_id,
          expires_at: T0,
        },
        201,
      ),
    );

  return api;
}

// -- social accounts (SMM-025B) ----------------------------------------------

export const ACCOUNT_ID = "66666666-6666-4666-8666-666666666666";
export const CREDENTIAL_ID = "77777777-7777-4777-8777-777777777777";

export function makeCredential(overrides: Partial<Credential> = {}): Credential {
  return {
    id: CREDENTIAL_ID,
    social_account_id: ACCOUNT_ID,
    status: "active",
    fingerprint: "3f9a1c0b77d2",
    key_id: "5d7c821189bf3a82",
    verification_status: "verified",
    verified_at: T0,
    expires_at: null,
    created_by_user_id: USER_ID,
    created_at: T0,
    superseded_at: null,
    revoked_at: null,
    ...overrides,
  };
}

export function makeAccount(overrides: Partial<SocialAccount> = {}): SocialAccount {
  return {
    id: ACCOUNT_ID,
    project_id: PROJECT_ID,
    platform: "threads",
    display_name: "Личный блог",
    status: "active",
    external_account_id: "1789",
    external_username: "travel_ru",
    version: 1,
    created_at: T0,
    updated_at: T0,
    disabled_at: null,
    credential: makeCredential(),
    publishing_ready: true,
    not_ready_reason: null,
    scheduled_publications: 0,
    ...overrides,
  };
}

// -- two projects (SMM-025A) -------------------------------------------------

export const PROJECT_B_ID = "beta";
export const ITEM_B_ID = "55555555-5555-4555-8555-555555555555";
export const PUBLICATION_B_ID = 77;

/** A response held back until the test releases it. */
export function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });

  return { promise, resolve };
}

export function makeBetaProject(role: Role, isActive = true): Project {
  return {
    ...makeProject(role),
    id: PROJECT_B_ID,
    display_name: "Beta Project",
    default_timezone: "Europe/Berlin",
    is_active: isActive,
  };
}

/**
 * Two projects with clearly different data: "demo" (Demo Project) and
 * "beta" (Beta Project). Any other project id answers 404, as the API
 * does for an unknown project and for one without a membership.
 */
export function createTwoProjectServer(
  options: {
    roles?: { demo: Role; beta: Role };
    betaActive?: boolean;
    mutationsEnabled?: boolean;
  } = {},
): FakeApi {
  const roles = options.roles ?? { demo: "owner", beta: "viewer" };
  const mutationsEnabled = options.mutationsEnabled ?? true;
  const server = createServer(roles.demo, { mutationsEnabled });

  const projects: Record<string, Project> = {
    [PROJECT_ID]: makeProject(roles.demo),
    [PROJECT_B_ID]: makeBetaProject(roles.beta, options.betaActive ?? true),
  };
  const items: Record<string, ContentItemDetail> = {
    [PROJECT_ID]: makeItem("draft", { title: "Demo post" }),
    [PROJECT_B_ID]: makeItem("draft", { id: ITEM_B_ID, project_id: PROJECT_B_ID, title: "Beta post" }),
  };
  const publications: Record<string, PublicationDetail> = {
    [PROJECT_ID]: makePublication("failed", { title: "Demo snapshot" }),
    [PROJECT_B_ID]: makePublication("failed", {
      id: PUBLICATION_B_ID,
      project_id: PROJECT_B_ID,
      title: "Beta snapshot",
    }),
  };
  const notFound = {
    status: 404,
    body: { error: { code: "NOT_FOUND", message: "Project not found.", details: {}, request_id: "r" } },
  };
  const known = (id: string | undefined) => id !== undefined && id in projects;

  server
    .on("GET", "/api/v1/auth/me", ok({
      ...makeMe(roles.demo, mutationsEnabled),
      projects: Object.values(projects).map((p) => ({
        project_id: p.id,
        display_name: p.display_name,
        role: p.role,
      })),
    }))
    .on("GET", "/api/v1/projects", ok(Object.values(projects)))
    .on("GET", "/api/v1/projects/:projectId", (req) =>
      known(req.params.projectId) ? ok(projects[req.params.projectId!]) : notFound,
    )
    .on("GET", "/api/v1/projects/:projectId/operations/summary", (req) =>
      known(req.params.projectId)
        ? ok(makeSummary({ project_id: req.params.projectId!, api_mutations_enabled: mutationsEnabled }))
        : notFound,
    )
    .on("GET", "/api/v1/projects/:projectId/content-items", (req) =>
      known(req.params.projectId) ? ok(page([items[req.params.projectId!]])) : notFound,
    )
    .on("GET", "/api/v1/projects/:projectId/publications", (req) =>
      known(req.params.projectId) ? ok(page([publications[req.params.projectId!]])) : notFound,
    )
    .on("GET", "/api/v1/projects/:projectId/operations/attention", (req) =>
      known(req.params.projectId) ? ok(page([publications[req.params.projectId!]])) : notFound,
    )
    .on("GET", "/api/v1/projects/:projectId/audit", (req) =>
      known(req.params.projectId) ? ok(page([])) : notFound,
    )
    .on("GET", "/api/v1/content-items/:id", (req) =>
      ok(req.params.id === ITEM_B_ID ? items[PROJECT_B_ID] : items[PROJECT_ID]),
    )
    .on("GET", "/api/v1/publications/:id", (req) =>
      ok(Number(req.params.id) === PUBLICATION_B_ID ? publications[PROJECT_B_ID] : publications[PROJECT_ID]),
    );

  return server;
}
