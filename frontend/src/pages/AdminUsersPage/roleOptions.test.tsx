/**
 * Shared frontend role-option helper contract (issue #560, check C3).
 *
 * The fix adds `frontend/src/pages/AdminUsersPage/roleOptions.ts` exporting:
 *
 *   export function roleOptionsFor(
 *     actorRole: UserRole,
 *     opts: { targetRole?: UserRole | null; mode: 'create' | 'edit' },
 *   ): { value: UserRole; label: string }[]
 *
 * These assertions pin that the TS rule mirrors the backend rule (check C1):
 * any actual role change on an existing user, and any grant of
 * admin/superadmin at creation, requires a superadmin actor; admins may
 * create member/viewer users and may only re-select a target's current
 * role when editing (no alternatives offered).
 *
 * Labels mirror the ROLE_OPTIONS map in CreateUserDialog.tsx:31-36:
 * superadmin -> 'Super Admin', admin -> 'Admin', member -> 'Member',
 * viewer -> 'Viewer'.
 *
 * Base-expected outcome: ERROR (NEW-SURFACE) — the module under test does
 * not exist yet, so vitest fails to resolve './roleOptions'. Pure function
 * test; no rendering, no extra dependencies.
 */
import { describe, expect, it } from 'vitest';
import type { UserRole } from './types';
import { roleOptionsFor } from './roleOptions';

const LABELS: Record<UserRole, string> = {
  superadmin: 'Super Admin',
  admin: 'Admin',
  member: 'Member',
  viewer: 'Viewer',
};

const ALL_ROLES: UserRole[] = ['superadmin', 'admin', 'member', 'viewer'];

function values(options: { value: UserRole; label: string }[]): UserRole[] {
  return options.map((option) => option.value);
}

describe('roleOptionsFor', () => {
  it('admin creation options are member and viewer only', () => {
    const options = roleOptionsFor('admin', { mode: 'create' });
    expect(values(options)).toEqual(['member', 'viewer']);
  });

  it('admin edit options for a member target are the current role only (no alternatives)', () => {
    const options = roleOptionsFor('admin', { mode: 'edit', targetRole: 'member' });
    expect(values(options)).toEqual(['member']);
  });

  it('admin edit options for an admin target are the current role only (Edit-dialog no-op parity with the backend)', () => {
    const options = roleOptionsFor('admin', { mode: 'edit', targetRole: 'admin' });
    expect(values(options)).toEqual(['admin']);
  });

  it('admin edit options for a viewer target are the current role only', () => {
    const options = roleOptionsFor('admin', { mode: 'edit', targetRole: 'viewer' });
    expect(values(options)).toEqual(['viewer']);
  });

  it('admin edit options fall back to member and viewer when targetRole is null', () => {
    const options = roleOptionsFor('admin', { mode: 'edit', targetRole: null });
    expect(values(options)).toEqual(['member', 'viewer']);
  });

  it('superadmin creation options are all four roles in order', () => {
    const options = roleOptionsFor('superadmin', { mode: 'create' });
    expect(values(options)).toEqual(ALL_ROLES);
  });

  it('superadmin edit options are all four roles in order', () => {
    const options = roleOptionsFor('superadmin', { mode: 'edit', targetRole: 'member' });
    expect(values(options)).toEqual(ALL_ROLES);
  });

  it('labels mirror the ROLE_OPTIONS label map', () => {
    const optionSets = [
      roleOptionsFor('superadmin', { mode: 'create' }),
      roleOptionsFor('superadmin', { mode: 'edit', targetRole: 'viewer' }),
      roleOptionsFor('admin', { mode: 'create' }),
      roleOptionsFor('admin', { mode: 'edit', targetRole: 'member' }),
    ];
    for (const options of optionSets) {
      for (const option of options) {
        expect(option.label).toBe(LABELS[option.value]);
      }
    }
  });
});
