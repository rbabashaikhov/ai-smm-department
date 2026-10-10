// Mirrors of the response bodies in src/ai_smm/api/schemas.py.
// Datetimes arrive as ISO 8601 strings with an offset; ids of content rows
// are UUID strings, ids of publication rows are integers.

export type Role = "viewer" | "editor" | "admin" | "owner";

export type PublicationStatus =
  | "draft"
  | "approved"
  | "scheduled"
  | "claimed"
  | "publishing"
  | "published"
  | "failed"
  | "needs_review"
  | "cancelled";

export const PUBLICATION_STATUSES: readonly PublicationStatus[] = [
  "draft",
  "approved",
  "scheduled",
  "claimed",
  "publishing",
  "published",
  "failed",
  "needs_review",
  "cancelled",
];

export type ContentStatus =
  | "draft"
  | "in_review"
  | "approved"
  | "rejected"
  | "archived";

export const CONTENT_STATUSES: readonly ContentStatus[] = [
  "draft",
  "in_review",
  "approved",
  "rejected",
  "archived",
];

export type ContentFormat = "text" | "image" | "carousel" | "thread";

export const CONTENT_FORMATS: readonly ContentFormat[] = [
  "text",
  "image",
  "carousel",
  "thread",
];

export type RevisionSource =
  | "human"
  | "strategist"
  | "copywriter"
  | "editor"
  | "import";

export type ApprovalDecision = "approved" | "rejected";

export type JsonObject = Record<string, unknown>;

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface PageParams {
  limit: number;
  offset: number;
}

// -- auth --------------------------------------------------------------

export interface User {
  id: string;
  email: string;
  display_name: string;
  is_active: boolean;
  last_login_at: string | null;
  created_at: string;
}

export interface ProjectAccess {
  project_id: string;
  display_name: string;
  role: Role;
}

export interface MeResponse {
  user: User;
  projects: ProjectAccess[];
}

export interface LoginResponse {
  user: User;
  csrf_token: string;
}

export interface CsrfResponse {
  csrf_token: string;
  header_name: string;
}

// -- projects ----------------------------------------------------------

export interface Project {
  id: string;
  display_name: string;
  description: string;
  default_platform: string;
  default_timezone: string;
  default_language: string;
  is_active: boolean;
  version: number;
  created_at: string;
  updated_at: string;
  role: Role;
}

export interface ProjectSettings {
  project_id: string;
  content_config: JsonObject;
  publishing_config: JsonObject;
  brand_config: JsonObject;
  version: number;
  updated_at: string | null;
}

// -- publications ------------------------------------------------------

export interface PublicationSummary {
  id: number;
  project_id: string;
  platform: string;
  ordinal: number;
  title: string;
  format: string;
  status: PublicationStatus;
  scheduled_at: string | null;
  published_at: string | null;
  threads_post_id: string | null;
  attempt_count: number;
  human_reviewed: boolean;
  series_id: number | null;
  series_position: number | null;
  series_total: number | null;
  image_count: number;
  body_chars: number;
  created_at: string;
  updated_at: string;
}

export interface PublicationDetail extends PublicationSummary {
  body: string;
  images: JsonObject[];
  items: JsonObject[];
  editor_score: number | null;
  last_error: string | null;
  next_attempt_at: string | null;
  claimed_by: string | null;
  claimed_at: string | null;
  lease_expires_at: string | null;
  parent_publication_id: number | null;
  previous_publication_id: number | null;
  source_ref: string | null;
  idempotency_key: string;
}

export interface PublicationPreview {
  publication: PublicationDetail;
  content_valid: boolean;
  content_error: string | null;
  human_reviewed: boolean;
  series_ready: boolean;
  series_reason: string | null;
  blocking_parts: number[];
  publishable_now: boolean;
}

export interface Attempt {
  id: number;
  publication_id: number;
  attempt_number: number;
  worker_id: string;
  phase: string;
  outcome: string | null;
  started_at: string;
  finished_at: string | null;
  threads_creation_id: string | null;
  threads_post_id: string | null;
  http_status: number | null;
  error_type: string | null;
  error_message: string | null;
  details: JsonObject;
}

export interface PublicationFilters {
  status?: PublicationStatus[];
  format?: string;
  series_id?: number;
  human_reviewed?: boolean;
}

export interface ScheduleRequest {
  /** Must carry an explicit offset, e.g. 2026-10-12T09:00:00+03:00. */
  scheduled_at: string;
  note?: string;
  reset_attempts?: boolean;
}

export interface CancelRequest {
  note?: string;
}

// -- operations / audit ------------------------------------------------

export interface OperationsSummary {
  project_id: string;
  total: number;
  by_status: Record<string, number>;
  due_now: number;
  needs_attention: number;
  published_last_24h: number;
  last_published_at: string | null;
  next_scheduled_at: string | null;
  series_total: number;
  series_active: number;
  dry_run: boolean;
}

export interface AuditEntry {
  id: number;
  at: string;
  actor: string;
  action: string;
  subject: string | null;
  details: JsonObject;
}

// -- content -----------------------------------------------------------

export interface Revision {
  id: string;
  content_item_id: string;
  revision_number: number;
  title: string | null;
  body: string;
  format: ContentFormat;
  images: JsonObject[];
  items: JsonObject[];
  metadata: JsonObject;
  source: RevisionSource;
  source_ref: string | null;
  editor_score: number | null;
  editor_notes: string;
  content_hash: string;
  created_by_user_id: string | null;
  created_at: string;
}

export interface ContentItemSummary {
  id: string;
  project_id: string;
  title: string;
  content_type: string;
  status: ContentStatus;
  current_revision_id: string;
  approved_revision_id: string | null;
  version: number;
  created_by_user_id: string | null;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
}

export interface ContentItemDetail extends ContentItemSummary {
  current_revision: Revision;
  /** True only when the current revision is the approved one. */
  approval_in_effect: boolean;
}

export interface Approval {
  id: string;
  content_item_id: string;
  revision_id: string;
  decision: ApprovalDecision;
  actor_user_id: string;
  note: string;
  created_at: string;
}

export interface RevisionInput {
  title?: string | null;
  body: string;
  format: ContentFormat;
  images?: JsonObject[];
  items?: JsonObject[];
  metadata?: JsonObject;
  source?: RevisionSource;
  source_ref?: string | null;
  editor_score?: number | null;
  editor_notes?: string;
}

export interface ContentItemCreate {
  title: string;
  content_type: string;
  revision: RevisionInput;
}

export interface RevisionCreate extends RevisionInput {
  expected_item_version: number;
}

export interface RevisionDecision {
  revision_id: string;
  expected_item_version?: number;
  note?: string;
}

export interface RevisionCreated {
  item: ContentItemSummary;
  revision: Revision;
}

export interface DecisionResult {
  item: ContentItemSummary;
  approval: Approval;
}

export interface MaterializeResult {
  result: "created" | "unchanged";
  platform: string;
  revision_id: string;
  publication: PublicationDetail;
}

export interface ContentFilters {
  status?: ContentStatus[];
  content_type?: string;
}
