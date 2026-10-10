// What the UI offers to each role. This is presentation only: the backend
// checks the same rules on every request and remains the authority. A
// control shown by mistake still gets a 403.
//
// Mirrors the RBAC table in docs/api.md.

import type { Role } from "../../types/api";

const RANK: Record<Role, number> = { viewer: 0, editor: 1, admin: 2, owner: 3 };

export const atLeast = (role: Role | undefined, minimum: Role): boolean =>
  role !== undefined && RANK[role] >= RANK[minimum];

export const permissions = (role: Role | undefined) => ({
  /** create item / revision, submit, approve, reject, materialize */
  canEditContent: atLeast(role, "editor"),
  /** schedule / reschedule / cancel */
  canCommandPublications: atLeast(role, "admin"),
  canReadAudit: atLeast(role, "admin"),
});

export type Permissions = ReturnType<typeof permissions>;
