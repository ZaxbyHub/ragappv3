"""Shared vault authorization policy (service layer).

Issue #480 (D3): the core vault-permission evaluation previously lived in
``app.api.deps`` (the FastAPI dependency layer). A service-layer module
(``app.services.vision_evidence``) reached back into ``app.api.deps`` for it —
a services→api dependency inversion. The canonical evaluator + permission
helpers + level maps now live HERE (service layer), and ``app.api.deps``
re-imports them so its existing public symbols and FastAPI callers are
unchanged.

This module has NO FastAPI/request dependencies: it operates on a plain
``sqlite3.Connection`` and a principal dict, so any service may call it without
inverting the dependency direction.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3

logger = logging.getLogger(__name__)

# SQLite thread-safety guard for parallelized vault permission queries.
# asyncio.to_thread runs callables in separate OS threads; sharing a single
# sqlite3.Connection across those threads is only safe when SQLite is built
# with SERIALIZED threading (sqlite3.threadsafety == 3). Otherwise, fall back
# to sequential to_thread calls to avoid "SQLite objects created in a thread
# can only be used in that same thread" and subtle data corruption.
_SQLITE_SERIALIZED = sqlite3.threadsafety == 3
_FALLBACK_WARNED = False


def _warn_fallback_threading() -> None:
    """Log the sequential fallback warning once per process."""
    global _FALLBACK_WARNED
    if _FALLBACK_WARNED:
        return
    _FALLBACK_WARNED = True
    logger.warning(
        "SQLite threading mode is %s; get_effective_vault_permissions will "
        "run sequentially. For parallel execution, use a SERIALIZED build "
        "(sqlite3.threadsafety == 3).",
        sqlite3.threadsafety,
    )


VAULT_PERMISSION_LEVELS = {"read": 1, "write": 2, "admin": 3}
VAULT_PERMISSION_NAMES = {0: None, 1: "read", 2: "write", 3: "admin"}
VAULT_ACTION_LEVELS = {"read": 1, "write": 2, "delete": 3, "admin": 3}


def direct_vault_admin_ids(
    conn: sqlite3.Connection,
    vault_id: int,
    exclude_user_id: int | None = None,
) -> set[int]:
    """Return the user ids holding DIRECT admin permission on a vault.

    One layer of the shared effective-admin inventory (issue #1025, PRR-010):
    the last-admin guards on the member and group-access routes must count the
    same admins the policy evaluator grants, so the direct layer is exactly the
    ``vault_members`` admin rows ``get_effective_vault_permissions`` merges.
    ``exclude_user_id`` models a member's removal/demotion (their direct row is
    going away); group-derived coverage is NOT excluded here — a demoted member
    keeps any group grant.

    The inventory counts GRANT ROWS ONLY (F-006): it does not model the
    superadmin role baseline (``evaluate`` grants superadmins admin on every
    vault) nor ``users.is_active``. A vault whose sole counted coverage is a
    superadmin or a deactivated account therefore has an EMPTY inventory here,
    and the guards built on it can refuse conservatively (fail-closed).
    Adding a vault member (or reactivating the account) remains the repair
    path; no behavior change rides on this docstring.
    """
    sql = (
        "SELECT user_id FROM vault_members"
        " WHERE vault_id = ? AND permission = 'admin'"
    )
    params: list = [vault_id]
    if exclude_user_id is not None:
        sql += " AND user_id != ?"
        params.append(exclude_user_id)
    return {row[0] for row in conn.execute(sql, params).fetchall()}


def group_derived_vault_admin_ids(
    conn: sqlite3.Connection,
    vault_id: int,
    exclude_user_id: int | None = None,
    exclude_group_id: int | None = None,
) -> set[int]:
    """Return the user ids holding GROUP-DERIVED admin permission on a vault.

    The other inventory layer, mirroring ``_query_group_access``'s join exactly
    (``vault_group_access`` x ``group_members`` x ``groups`` with the
    ``org_members`` EXISTS that gates derived access on org membership) so a
    grant counted here is a grant the policy evaluator honors, and vice versa.
    ``exclude_group_id`` models one group's grant being revoked or demoted
    (that grant no longer confers admin); ``exclude_user_id`` models a user's
    disappearance from every group (user deletion).

    The inventory counts GRANT ROWS ONLY (F-006): it does not model the
    superadmin role baseline (``evaluate`` grants superadmins admin on every
    vault) nor ``users.is_active``. When a vault's sole counted coverage is a
    superadmin or a deactivated account this layer reports nothing, and the
    guards built on it can refuse conservatively (fail-closed). Adding a
    vault member (or reactivating the account) remains the repair path; no
    behavior change rides on this docstring.
    """
    sql = """SELECT DISTINCT gm.user_id FROM vault_group_access vga
        JOIN group_members gm ON vga.group_id = gm.group_id
        JOIN groups g ON g.id = gm.group_id
        WHERE vga.vault_id = ? AND vga.permission = 'admin'"""
    params: list = [vault_id]
    if exclude_group_id is not None:
        sql += " AND vga.group_id != ?"
        params.append(exclude_group_id)
    sql += """ AND EXISTS (
            SELECT 1 FROM org_members om
            WHERE om.org_id = g.org_id AND om.user_id = gm.user_id
        )"""
    if exclude_user_id is not None:
        sql += " AND gm.user_id != ?"
        params.append(exclude_user_id)
    return {row[0] for row in conn.execute(sql, params).fetchall()}


async def get_effective_vault_permission(
    db: sqlite3.Connection,
    principal: dict,
    vault_id: int | None,
) -> str | None:
    """Return the user's strongest effective permission on a vault."""
    if vault_id is None:
        return None
    result = await get_effective_vault_permissions(db, principal, [vault_id])
    return result.get(vault_id)


async def get_effective_vault_permissions(
    db: sqlite3.Connection,
    principal: dict,
    vault_ids: list[int],
) -> dict[int, str | None]:
    """Return each vault's strongest effective permission for the user."""
    user_id = principal.get("id")
    user_role = principal.get("role", "")
    normalized_ids = list(dict.fromkeys(vault_ids))

    if user_id is None or not normalized_ids:
        return {}

    if user_role == "superadmin":
        return {vault_id: "admin" for vault_id in normalized_ids}

    baseline_level = VAULT_PERMISSION_LEVELS["write"] if user_role == "admin" else 0
    effective_levels = {vault_id: baseline_level for vault_id in normalized_ids}
    placeholders = ",".join("?" for _ in normalized_ids)

    def _query_vault_members():
        return db.execute(
            f"""SELECT vault_id, permission FROM vault_members
                WHERE user_id = ? AND vault_id IN ({placeholders})""",
            (user_id, *normalized_ids),
        ).fetchall()

    def _query_group_access():
        return db.execute(
            f"""SELECT vga.vault_id, vga.permission FROM vault_group_access vga
               JOIN group_members gm ON vga.group_id = gm.group_id
               JOIN groups g ON g.id = gm.group_id
               WHERE gm.user_id = ? AND vga.vault_id IN ({placeholders})
                 AND EXISTS (
                     SELECT 1 FROM org_members om
                     WHERE om.org_id = g.org_id AND om.user_id = ?
                 )""",
            (user_id, *normalized_ids, user_id),
        ).fetchall()

    def _query_public_vaults():
        return db.execute(
            f"""SELECT v.id FROM vaults v
                WHERE v.visibility = 'public' AND v.id IN ({placeholders})
                AND (v.org_id IS NULL OR EXISTS (
                    SELECT 1 FROM org_members WHERE org_id = v.org_id AND user_id = ?
                ))""",
            (*normalized_ids, user_id),
        ).fetchall()

    def _query_org_vaults():
        return db.execute(
            f"""SELECT v.id FROM vaults v
                WHERE v.visibility = 'org' AND v.id IN ({placeholders})
                AND v.org_id IS NOT NULL
                AND EXISTS (
                    SELECT 1 FROM org_members WHERE org_id = v.org_id AND user_id = ?
                )""",
            (*normalized_ids, user_id),
        ).fetchall()

    if _SQLITE_SERIALIZED:
        members_rows, group_rows, public_rows, org_rows = await asyncio.gather(
            asyncio.to_thread(_query_vault_members),
            asyncio.to_thread(_query_group_access),
            asyncio.to_thread(_query_public_vaults),
            asyncio.to_thread(_query_org_vaults),
        )
    else:
        _warn_fallback_threading()
        members_rows = await asyncio.to_thread(_query_vault_members)
        group_rows = await asyncio.to_thread(_query_group_access)
        public_rows = await asyncio.to_thread(_query_public_vaults)
        org_rows = await asyncio.to_thread(_query_org_vaults)

    for vault_id, permission in members_rows:
        effective_levels[vault_id] = max(
            effective_levels[vault_id],
            VAULT_PERMISSION_LEVELS.get(permission, 0),
        )

    for vault_id, permission in group_rows:
        effective_levels[vault_id] = max(
            effective_levels[vault_id],
            VAULT_PERMISSION_LEVELS.get(permission, 0),
        )

    for (vault_id,) in public_rows:
        effective_levels[vault_id] = max(
            effective_levels[vault_id],
            VAULT_PERMISSION_LEVELS["read"],
        )

    for (vault_id,) in org_rows:
        effective_levels[vault_id] = max(
            effective_levels[vault_id],
            VAULT_PERMISSION_LEVELS["read"],
        )

    return {
        vault_id: VAULT_PERMISSION_NAMES.get(effective_levels[vault_id])
        for vault_id in normalized_ids
    }


async def evaluate_policy(
    db: sqlite3.Connection,
    principal: dict,
    resource_type: str,
    resource_id: int | None,
    action: str,
) -> bool:
    """Core policy evaluation logic with injected database connection.

    This is the canonical service-layer evaluator. ``app.api.deps._evaluate_policy``
    is a thin alias for it so existing FastAPI callers are unchanged.
    """
    user_id = principal.get("id")
    user_role = principal.get("role", "")

    if user_id is None:
        return False

    if resource_type not in ("vault", "group"):
        return user_role == "superadmin"

    # Group resources: admin and superadmin have full access
    if resource_type == "group":
        return user_role in ("superadmin", "admin")

    if resource_id is None:
        return False

    if user_role == "superadmin":
        return True

    effective_permission = await get_effective_vault_permission(db, principal, resource_id)
    effective_level = VAULT_PERMISSION_LEVELS.get(effective_permission or "", 0)
    required_level = VAULT_ACTION_LEVELS.get(action, VAULT_ACTION_LEVELS["read"])
    return effective_level >= required_level
