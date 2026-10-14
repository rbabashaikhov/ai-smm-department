// What the UI offers to each role. This is presentation only: the backend
// checks the same rules on every request and remains the authority. A
// control shown by mistake still gets a 403.
//
// Mirrors the RBAC table in docs/api.md. Since SMM-025D every business
// change is the project OWNER's (the API's dependencies and PostgreSQL's
// guards refuse anyone else); editor and admin read, admin also the audit
// trail. On a read-only deployment
// (control_plane.mutations_enabled = false) no role is offered a write
// control at all -- the server refuses them with MUTATIONS_DISABLED, so
// offering one would only produce an error.

import type { Role } from "../../types/api";

const RANK: Record<Role, number> = { viewer: 0, editor: 1, admin: 2, owner: 3 };

export const atLeast = (role: Role | undefined, minimum: Role): boolean =>
  role !== undefined && RANK[role] >= RANK[minimum];

export const permissions = (role: Role | undefined, mutationsEnabled: boolean) => ({
  /** The deployment refuses every change, whatever the role. */
  readOnly: !mutationsEnabled,
  /** create item / revision, submit, approve, reject, materialize */
  canEditContent: mutationsEnabled && atLeast(role, "owner"),
  /** schedule / reschedule / cancel */
  canCommandPublications: mutationsEnabled && atLeast(role, "owner"),
  canReadAudit: atLeast(role, "admin"),
  /** project name, description, timezone, topics, tone */
  canEditSettings: mutationsEnabled && atLeast(role, "owner"),
  /** accounts and credentials: which identity the project speaks as */
  canManageAccounts: mutationsEnabled && atLeast(role, "owner"),
});

/** POST /projects: the server allows it to the owner of any project. */
export const canCreateProject = (roles: Role[], mutationsEnabled: boolean): boolean =>
  mutationsEnabled && roles.some((role) => role === "owner");

export type Permissions = ReturnType<typeof permissions>;
