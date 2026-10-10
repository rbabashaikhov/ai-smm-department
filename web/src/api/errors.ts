// One error type for every failed request, built from the API's envelope:
//   {"error": {"code", "message", "details", "request_id"}}
// Components switch on `code` (and on the typed guards below), never on
// message text.

import type { PublicationStatus } from "../types/api";

export const ErrorCode = {
  AUTH_REQUIRED: "AUTH_REQUIRED",
  INVALID_CREDENTIALS: "INVALID_CREDENTIALS",
  FORBIDDEN: "FORBIDDEN",
  CSRF_REQUIRED: "CSRF_REQUIRED",
  CSRF_INVALID: "CSRF_INVALID",
  NOT_FOUND: "NOT_FOUND",
  VALIDATION_ERROR: "VALIDATION_ERROR",
  VERSION_CONFLICT: "VERSION_CONFLICT",
  INVALID_STATE_TRANSITION: "INVALID_STATE_TRANSITION",
  STALE_REVISION: "STALE_REVISION",
  PUBLICATION_NOT_EDITABLE: "PUBLICATION_NOT_EDITABLE",
  DEPENDENCY_UNAVAILABLE: "DEPENDENCY_UNAVAILABLE",
  METHOD_NOT_ALLOWED: "METHOD_NOT_ALLOWED",
  INTERNAL_ERROR: "INTERNAL_ERROR",
  // Client-side only: the request never produced an HTTP response, or the
  // response was not the API's envelope (a proxy page, for example).
  NETWORK_ERROR: "NETWORK_ERROR",
  UNEXPECTED_RESPONSE: "UNEXPECTED_RESPONSE",
} as const;

export type ErrorCodeValue = (typeof ErrorCode)[keyof typeof ErrorCode];

export interface VersionConflictDetails {
  expected_version: number;
  current_version: number;
}

export interface StaleRevisionDetails {
  requested_revision_id: string;
  current_revision_id: string;
}

export interface InvalidTransitionDetails {
  command: string;
  status: string;
  allowed_from: string[];
}

export interface PublicationNotEditableDetails {
  command: "materialize";
  publication_id: number;
  publication_status: PublicationStatus;
  previous_revision_id: string | null;
}

export interface ValidationDetails {
  fields?: { field: string; rule: string }[];
  content_error?: string;
}

export interface UnavailableDetails {
  reason?: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;
  readonly requestId: string | null;
  /** Seconds from the Retry-After header, when the server sent one. */
  readonly retryAfter: number | null;

  constructor(init: {
    status: number;
    code: string;
    message: string;
    details?: Record<string, unknown>;
    requestId?: string | null;
    retryAfter?: number | null;
  }) {
    super(init.message);
    this.name = "ApiError";
    this.status = init.status;
    this.code = init.code;
    this.details = init.details ?? {};
    this.requestId = init.requestId ?? null;
    this.retryAfter = init.retryAfter ?? null;
  }

  get isUnauthorized(): boolean {
    return this.status === 401;
  }

  get isForbidden(): boolean {
    return this.status === 403;
  }

  get isNotFound(): boolean {
    return this.status === 404;
  }

  get isConflict(): boolean {
    return this.status === 409;
  }

  get isValidation(): boolean {
    return this.status === 422;
  }

  /** 503: the server asked to come back later; nothing was changed. */
  get isUnavailable(): boolean {
    return this.status === 503;
  }
}

/** An ApiError narrowed to one code, with that code's details shape. */
export type TypedApiError<D> = ApiError & { details: D & Record<string, unknown> };

function hasCode(error: unknown, code: string): error is ApiError {
  return error instanceof ApiError && error.code === code;
}

export const isApiError = (error: unknown): error is ApiError =>
  error instanceof ApiError;

export const isVersionConflict = (
  error: unknown,
): error is TypedApiError<VersionConflictDetails> =>
  hasCode(error, ErrorCode.VERSION_CONFLICT);

export const isStaleRevision = (
  error: unknown,
): error is TypedApiError<StaleRevisionDetails> =>
  hasCode(error, ErrorCode.STALE_REVISION);

export const isInvalidStateTransition = (
  error: unknown,
): error is TypedApiError<InvalidTransitionDetails> =>
  hasCode(error, ErrorCode.INVALID_STATE_TRANSITION);

export const isPublicationNotEditable = (
  error: unknown,
): error is TypedApiError<PublicationNotEditableDetails> =>
  hasCode(error, ErrorCode.PUBLICATION_NOT_EDITABLE);

export const isValidationError = (
  error: unknown,
): error is TypedApiError<ValidationDetails> =>
  hasCode(error, ErrorCode.VALIDATION_ERROR);

export const isUnavailable = (
  error: unknown,
): error is TypedApiError<UnavailableDetails> =>
  error instanceof ApiError && error.status === 503;

export const isUnauthorized = (error: unknown): error is ApiError =>
  error instanceof ApiError && error.status === 401;

/** Parse Retry-After: delta-seconds or an HTTP date. */
export function parseRetryAfter(
  value: string | null,
  now: number = Date.now(),
): number | null {
  if (!value) return null;

  const trimmed = value.trim();

  if (/^\d+$/.test(trimmed)) return Number(trimmed);

  const at = Date.parse(trimmed);

  if (Number.isNaN(at)) return null;

  return Math.max(0, Math.ceil((at - now) / 1000));
}
