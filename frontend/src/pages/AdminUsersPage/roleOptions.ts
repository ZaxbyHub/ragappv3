import type { UserRole } from "./types";

/**
 * The single rule for who may be assigned which users.role value from the
 * admin UI (issue #560 C22) — mirrors the backend `assert_can_assign_role`
 * helper in backend/app/api/routes/users.py:
 *
 * - Any actual role change on an existing user, and any grant of admin or
 *   superadmin at creation, requires a superadmin actor.
 * - Admins may create member and viewer accounts, and may only re-select a
 *   target's current role when editing (the backend treats a re-asserted
 *   role as a no-op so name edits never 403).
 *
 * `mode: "create"` derives the options for POST /users/ (no target yet);
 * `mode: "edit"` derives them for PATCH /users/{id} given the target's
 * current role.
 */
export const ROLE_LABELS: Record<UserRole, string> = {
  superadmin: "Super Admin",
  admin: "Admin",
  member: "Member",
  viewer: "Viewer",
};

const ALL_ROLES: UserRole[] = ["superadmin", "admin", "member", "viewer"];

export function roleOptionsFor(
  actorRole: UserRole,
  opts: { targetRole?: UserRole | null; mode: "create" | "edit" },
): { value: UserRole; label: string }[] {
  if (actorRole === "superadmin") {
    return ALL_ROLES.map((value) => ({ value, label: ROLE_LABELS[value] }));
  }
  if (opts.mode === "edit") {
    const targetRole = opts.targetRole ?? null;
    if (targetRole) {
      return [{ value: targetRole, label: ROLE_LABELS[targetRole] }];
    }
  }
  return [
    { value: "member", label: ROLE_LABELS.member },
    { value: "viewer", label: ROLE_LABELS.viewer },
  ];
}
