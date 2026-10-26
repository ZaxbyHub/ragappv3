"""Restore and verify a backup set created by scripts/backup_set.py.

Restore is ALL-OR-NOTHING (#728, T1-28-S2-03/S-05/S2-01 + ENH-01): every
manifest digest and the generation binding are verified in a private
STAGING directory first, unmanaged destination content (documents/,
uploads/, library/, uncovered vaults, ...) is copied into staging so it
survives the swap, stale SQLite ``-wal``/``-shm``/``-journal`` sidecars
are never carried into the restored tree, and only then is the staging
directory renamed over the destination in one swap. A failure at any
single write, copy or rename step leaves the destination byte-identical
to its pre-restore state or fully restored — never a mix. The tagged
LanceDB generations are checked out AND persisted (``checkout(tag)`` plus
``restore()``) after the swap so a FRESH connection serves the tagged
generation, and any persistence failure rolls the whole swap back.

A restore refuses to run while another connection holds the destination
database in a write transaction (stop the application first), refuses a
destination that is a symlink, and refuses while a leftover crash journal
from a previous interrupted restore exists (the journal's recorded phase
maps to the manual roll-forward / roll-back steps in docs/operations.md).

The CI restore drill is backend/tests/test_518_restore.py — it exercises
backup -> mutate -> restore -> verify end to end with fakes; the
all-or-nothing and WAL drill is backend/tests/test_e02_*.
"""

import argparse
import gc
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT))
# backend.app.config imports ``app.*`` absolutely (config.py L13), which
# only resolves with backend/ itself on the path — the documented
# repo-root invocation has no PYTHONPATH (issue #727 / T1-28-K-01; the
# same shape scripts/migrate_embeddings.py already ships).
sys.path.append(str(ROOT / "backend"))

from backend.app.config import settings
from backend.app.services.secret_manager import SecretManager
from scripts.backup_set import _restrict_permissions

logger = logging.getLogger(__name__)

MANIFEST_NAME = "backup_manifest.json"

# SQLite identifiers cannot be bound as parameters; every interpolated table
# name in this module is validated against this pattern first.
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

SQLITE_SIDECARS = ("app.db-wal", "app.db-shm", "app.db-journal")


class RestoreError(Exception):
    """A backup set failed verification or could not be restored."""


def _contained(base: Path, candidate: Path) -> bool:
    """True when ``candidate`` resolves inside ``base`` (no ``..`` escapes,
    no absolute-path hijacks — swarm review F-005)."""
    try:
        candidate.resolve().relative_to(base.resolve())
    except ValueError:
        return False
    return True


def _default_key_provider() -> tuple:
    key, version = SecretManager().get_aes_key()
    return (key, str(version))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fin:
        for chunk in iter(lambda: fin.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _count_sqlite_rows(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        total = 0
        for table in tables:
            if not _IDENTIFIER_RE.fullmatch(table):
                # Belt-and-braces: sqlite_master names come from the
                # decrypted backup, not user input, but identifiers cannot
                # be bound parameters — validate before interpolation.
                raise RestoreError(
                    f"unexpected table name in restored db: {table!r}"
                )
            total += conn.execute(
                f'SELECT COUNT(*) FROM "{table}"'  # nosec B608 — identifier validated against _IDENTIFIER_RE above; no values interpolated
            ).fetchone()[0]
        return total
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Destination guards: lock, journal, held-DB probe
# ---------------------------------------------------------------------------


class _RestoreLock:
    """O_EXCL lock file serializing concurrent restores of one destination."""

    def __init__(self, dest_root: Path):
        self.path = dest_root.parent / f"{dest_root.name}.restore.lock"
        self._fd = -1

    def acquire(self) -> None:
        try:
            self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise RestoreError(
                f"another restore appears to be in progress for this "
                f"destination ({self.path} exists); if no restore is "
                "running, remove the stale lock file and retry"
            ) from None
        except OSError as exc:
            raise RestoreError(
                f"could not create the restore lock {self.path}: {exc}"
            ) from exc
        os.write(self._fd, f"pid={os.getpid()}\n".encode("utf-8"))

    def release(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("could not remove the restore lock %s", self.path)

    def retain(self) -> None:
        """Keep the lock FILE (an uncertain outcome must block the next
        restore) while closing the handle so operators and tooling can
        remove the stale lock."""
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1
        logger.warning(
            "restore lock retained at %s: remove it manually after "
            "completing the documented crash recovery",
            self.path,
        )


def _find_leftover_journal(dest_root: Path) -> Optional[Path]:
    for path in sorted(dest_root.parent.glob(f"{dest_root.name}.restore-journal-*.json")):
        return path
    return None


def _refuse_on_leftover_journal(dest_root: Path) -> None:
    journal = _find_leftover_journal(dest_root)
    if journal is None:
        return
    raise RestoreError(
        f"an interrupted restore left a crash journal ({journal}); the "
        "destination's state is uncertain. Complete the recovery manually "
        "per docs/operations.md (Restore crash recovery). The DISK, not "
        "the journal's phase field, is authoritative: if the aside path "
        "recorded in the journal exists, the original destination sits "
        "there (roll forward = aside is dispensable after rename staging "
        "-> dest + checkout(tag)/restore() per manifest table; roll back "
        "= rename the aside back over the destination); otherwise no "
        "rename happened (roll forward = rename dest aside then staging "
        "-> dest; roll back = leave the destination alone). Then delete "
        "the aside (when one remains), the journal and the lock, and "
        "retry."
    )


def _is_link_or_junction(path: Path) -> bool:
    """A symlink OR a Windows junction/reparse point (is_symlink() alone is
    false for junctions)."""
    if path.is_symlink():
        return True
    try:
        os.readlink(path)
        return True
    except (OSError, ValueError):
        return False


def _probe_destination_db_held(dest_db: Path) -> None:
    """Refuse before writing anything when another connection holds the
    destination database in a write transaction (#728 AC6; the documented
    stop-the-app precondition, enforced).

    The probe must leave the destination byte-identical in EVERY outcome.
    Closing the LAST connection to a WAL database checkpoints it —
    mutating ``app.db`` and deleting the ``-wal``/``-shm`` sidecars — so
    ``app.db`` and any sidecars are snapshotted to KEEPS OUTSIDE the
    destination (never inside it) and restored byte-identically around
    the probe. A sidecar that cannot be read while a writer holds it
    (Windows locks ``-shm`` exclusively) is simply not snapshotted: in
    that held case the probe is never the last connection, so no
    checkpoint occurs and there is nothing to restore. A destination
    database that is not readable SQLite (a corrupt file — the canonical
    disaster-recovery input) cannot be held in any meaningful sense, so
    the probe is skipped and the restore proceeds to overwrite it, the
    pre-change behavior."""
    if not dest_db.exists():
        return
    keep_dir = dest_db.parent.parent
    token = secrets.token_hex(4)
    saved: List[Tuple[Path, Path]] = []

    def _snapshot(path: Path) -> Optional[Path]:
        keep = keep_dir / f"{path.name}.probe-keep-{token}"
        try:
            shutil.copy2(path, keep, follow_symlinks=False)
        except OSError:
            # Locked by a live writer (e.g. -shm under an active hold) or
            # unreadable: nothing to preserve — see docstring.
            try:
                keep.unlink()
            except OSError:
                pass
            return None
        return keep

    try:
        keep = _snapshot(dest_db)
        if keep is not None:
            saved.append((keep, dest_db))
        for suffix in ("-wal", "-shm", "-journal"):
            side = dest_db.with_name(dest_db.name + suffix)
            if side.exists():
                keep = _snapshot(side)
                if keep is not None:
                    saved.append((keep, side))
        try:
            conn = sqlite3.connect(str(dest_db), timeout=0.0)
        except sqlite3.DatabaseError as exc:
            logger.info(
                "destination database %s is not readable SQLite (%s); "
                "skipping the held-database probe — the restore will "
                "overwrite it",
                dest_db,
                exc,
            )
            return
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError as exc:
            raise RestoreError(
                "the destination database is held by another connection "
                f"({dest_db}): stop the application before restoring "
                f"({exc})"
            ) from exc
        except sqlite3.DatabaseError as exc:
            # connect() is lazy; a corrupt header surfaces here.
            logger.info(
                "destination database %s is not readable SQLite (%s); "
                "skipping the held-database probe — the restore will "
                "overwrite it",
                dest_db,
                exc,
            )
            return
        finally:
            conn.close()
    finally:
        for keep, original in saved:
            try:
                if not original.exists():
                    # The probe's close deleted the file (checkpointed WAL
                    # sidecar): restore the exact pre-probe bytes.
                    os.replace(keep, original)
                    continue
                same = original.stat().st_size == keep.stat().st_size and (
                    _sha256_file(original) == _sha256_file(keep)
                )
                if same:
                    keep.unlink()
                else:
                    # The probe's close CHECKPOINTED the WAL into the main
                    # file: restore the pre-probe bytes so the destination
                    # stays byte-identical.
                    os.replace(keep, original)
            except OSError:
                logger.warning(
                    "could not restore the destination file %s after the "
                    "held-database probe (a live writer holds it; the "
                    "probe was not the last connection, so it made no "
                    "changes)",
                    original,
                )
                try:
                    keep.unlink()
                except OSError:
                    pass


def _write_journal(dest_root: Path, payload: dict) -> Path:
    journal = dest_root.parent / (
        f"{dest_root.name}.restore-journal-{secrets.token_hex(4)}.json"
    )
    try:
        journal.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as exc:
        raise RestoreError(
            f"could not write the restore crash journal {journal}: {exc}"
        ) from exc
    return journal


def _update_journal_phase(journal: Path, phase: str) -> None:
    payload = json.loads(journal.read_text(encoding="utf-8"))
    payload["phase"] = phase
    try:
        journal.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as exc:
        raise RestoreError(
            f"could not update the restore crash journal {journal}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Managed-path semantics for unmanaged-content preservation
# ---------------------------------------------------------------------------


def _managed_roots(manifest: dict) -> List[str]:
    """Destination-relative paths this restore owns (posix form).

    The sqlite item's artifact is ``app.db.enc`` but its DESTINATION name
    is ``app.db`` — bound ONLY when the manifest actually carries a
    sqlite item, so a (degenerate) sqlite-less manifest leaves an
    existing destination database as unmanaged content that is copied
    through the swap instead of silently dropped (#728 review finding);
    every tree item restores to its manifest path.
    """
    roots: List[str] = []
    for item in manifest.get("items", []):
        if item.get("kind") == "sqlite":
            roots.append("app.db")
            continue
        path = str(item.get("path", "")).replace("\\", "/")
        if path:
            roots.append(path)
    return roots


def _rel_parts(rel: str) -> Tuple[str, ...]:
    return tuple(part for part in rel.split("/") if part)


def _is_covered(rel: str, roots: List[str]) -> bool:
    """``rel`` equals or lies INSIDE a managed root."""
    relp = _rel_parts(rel)
    for root in roots:
        rootp = _rel_parts(root)
        if relp[: len(rootp)] == rootp:
            return True
    return False


def _is_partial(rel: str, roots: List[str]) -> bool:
    """``rel`` is a proper ANCESTOR of a managed root (e.g. ``vaults``
    when ``vaults/7`` is managed) — recurse so uncovered siblings survive
    while covered content is neither copied nor resurrected."""
    relp = _rel_parts(rel)
    for root in roots:
        rootp = _rel_parts(root)
        if len(relp) < len(rootp) and rootp[: len(relp)] == relp:
            return True
    return False


def _copy_unmanaged_into_staging(
    dest_root: Path, staging: Path, roots: List[str]
) -> None:
    """Copy destination content this restore does NOT own into staging.

    Runs while the destination is still untouched, so any fault here
    leaves the destination byte-identical. SQLite sidecars are skipped
    ONLY at the destination root AND only when this restore REPLACES the
    database (``app.db`` is a managed root, i.e. the manifest carries a
    sqlite item): a set that does not cover the database preserves it,
    and then its live ``-wal``/``-shm`` are the journal of SURVIVING
    bytes, not stale sidecars — they are copied through the swap with
    the database (implementation-review round 2). A same-named file
    inside an uncovered subtree is ordinary unmanaged content. Symlinks
    are preserved AS LINKS (never followed); a junction/reparse-point
    directory inside an unmanaged tree refuses the whole restore before
    any rename — it is neither portably preservable nor safe to
    traverse.
    """
    def refuse_junction(path: Path) -> None:
        try:
            os.readlink(path)
            is_link = path.is_symlink()
        except OSError:
            return
        if not is_link:
            # readlink succeeded but is_symlink() is false: a Windows
            # junction/reparse point.
            raise RestoreError(
                f"unmanaged destination entry {path} is a junction/"
                "reparse point; restore refuses to traverse or silently "
                "dereference it — remove or replace the link, then retry"
            )

    def scan_for_junctions(root: Path) -> None:
        """Refuse junctions at ANY depth of an unmanaged subtree BEFORE
        copytree traverses it: shutil.copytree(symlinks=True) preserves
        symlinks but recurses INTO junctions (DirEntry.is_symlink() is
        false for MOUNT_POINT reparse points), silently dereferencing
        them (#728 review finding)."""
        stack = [root]
        while stack:
            current = stack.pop()
            for entry in os.scandir(current):
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    try:
                        os.readlink(entry.path)
                    except OSError:
                        stack.append(entry.path)
                        continue
                    # readlink succeeded on a non-symlink dir: a junction.
                    raise RestoreError(
                        f"unmanaged destination entry {entry.path} is a "
                        "junction/reparse point; restore refuses to "
                        "traverse or silently dereference it — remove or "
                        "replace the link, then retry"
                    )

    drop_sidecars = "app.db" in roots

    def walk(directory: Path, rel_prefix: str) -> None:
        for entry in sorted(directory.iterdir()):
            rel = f"{rel_prefix}{entry.name}"
            if drop_sidecars and not rel_prefix and entry.name in SQLITE_SIDECARS:
                continue
            if _is_covered(rel, roots):
                continue
            if entry.is_symlink():
                # Preserve the LINK itself, never read its target.
                target = os.readlink(entry)
                link_dst = staging / Path(rel)
                link_dst.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(
                    target, str(link_dst), target_is_directory=entry.is_dir()
                )
                continue
            if entry.is_dir():
                refuse_junction(entry)
                if _is_partial(rel, roots):
                    walk(entry, f"{rel}/")
                else:
                    scan_for_junctions(entry)
                    shutil.copytree(
                        entry,
                        staging / Path(rel),
                        symlinks=True,
                    )
                continue
            shutil.copy2(entry, staging / Path(rel), follow_symlinks=False)

    walk(dest_root, "")


def _unmanaged_bytes(dest_root: Path, roots: List[str]) -> int:
    total = 0
    if not dest_root.exists():
        return 0

    drop_sidecars = "app.db" in roots

    def walk(directory: Path, rel_prefix: str) -> None:
        nonlocal total
        for entry in sorted(directory.iterdir()):
            rel = f"{rel_prefix}{entry.name}"
            if drop_sidecars and not rel_prefix and entry.name in SQLITE_SIDECARS:
                continue
            if _is_covered(rel, roots):
                continue
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if _is_partial(rel, roots):
                    walk(entry, f"{rel}/")
                else:
                    for f in entry.rglob("*"):
                        if f.is_file():
                            try:
                                total += f.stat().st_size
                            except OSError:
                                total += 0
            elif entry.is_file():
                try:
                    total += entry.stat().st_size
                except OSError:
                    total += 0

    walk(dest_root, "")
    return total


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


def restore_backup_set(
    backup_dir: Path,
    dest_root: Path,
    *,
    lancedb_factory: Optional[Callable[[str], object]] = None,
    key_provider: Optional[Callable[[], tuple]] = None,
) -> dict:
    backup_dir = Path(backup_dir)
    dest_root = Path(dest_root)
    manifest_path = backup_dir / MANIFEST_NAME
    if not manifest_path.exists():
        raise RestoreError(f"no {MANIFEST_NAME} in {backup_dir}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "ragapp-backup-set/1":
        raise RestoreError(
            f"unsupported manifest schema {manifest.get('schema')!r}"
        )

    # --- Guards (nothing written yet) ---
    if _is_link_or_junction(dest_root):
        raise RestoreError(
            f"destination {dest_root} is a symlink or junction; restore "
            "into a real directory"
        )
    if _contained(dest_root, backup_dir):
        raise RestoreError(
            f"backup set {backup_dir} lies inside the destination "
            f"{dest_root}; restore from a set stored outside the "
            "destination"
        )
    _refuse_on_leftover_journal(dest_root)
    # The lock/journal/staging siblings live in dest_root.parent — create
    # it BEFORE acquiring the lock so a fresh nested destination (one whose
    # parent does not exist yet, the base-restore behavior) works instead
    # of failing at lock creation (#728 review finding).
    dest_root.parent.mkdir(parents=True, exist_ok=True)
    lock = _RestoreLock(dest_root)
    lock.acquire()
    journal: Optional[Path] = None
    try:
        _probe_destination_db_held(dest_root / "app.db")

        provider = key_provider or _default_key_provider
        dest_existed = dest_root.exists()
        roots = _managed_roots(manifest)

        # --- Space guard: staging holds the set plus unmanaged copies ---
        set_bytes = sum(
            f.stat().st_size for f in backup_dir.rglob("*") if f.is_file()
        )
        extra_bytes = _unmanaged_bytes(dest_root, roots) if dest_existed else 0
        try:
            free_bytes = shutil.disk_usage(dest_root.parent).free
        except OSError:
            free_bytes = -1
        if free_bytes >= 0:
            needed = set_bytes + extra_bytes
            if free_bytes < needed:
                raise RestoreError(
                    f"insufficient space for a staged restore: the set is "
                    f"{set_bytes} bytes and {extra_bytes} bytes of "
                    f"unmanaged destination content must be copied; "
                    f"{free_bytes} bytes are free on {dest_root.parent}"
                )
            logger.info(
                "staged restore needs about %d bytes (set %d + unmanaged "
                "%d); %d free",
                needed,
                set_bytes,
                extra_bytes,
                free_bytes,
            )

        # --- Verify and stage EVERYTHING before touching the destination ---
        staging = dest_root.parent / (
            f"{dest_root.name}.staging-{secrets.token_hex(4)}"
        )
        if staging.exists():
            shutil.rmtree(staging)
        items_out = []
        sqlite_rows = 0
        verified = True

        try:
            staging.mkdir(parents=True)
            for item in manifest.get("items", []):
                kind = item.get("kind")
                name = item.get("name", "?")
                ok = True
                # Path containment (swarm review F-005): every manifest path
                # must stay inside the backup set on the source side and
                # inside the destination root on the restore side — a
                # crafted manifest must never rmtree/copytree outside
                # either root.
                item_path = str(item.get("path", ""))
                if not item_path:
                    raise RestoreError(f"manifest item {name!r} has no path")
                if not _contained(backup_dir, backup_dir / item_path):
                    raise RestoreError(
                        f"manifest path escapes the backup set: {item_path!r}"
                    )
                if not _contained(dest_root, dest_root / item_path):
                    raise RestoreError(
                        f"manifest path escapes the destination root: {item_path!r}"
                    )

                if kind == "sqlite":
                    artifact = backup_dir / item_path
                    if not artifact.exists():
                        raise RestoreError(f"sqlite artifact missing: {artifact}")
                    if _sha256_file(artifact) != item.get("sha256"):
                        raise RestoreError(
                            f"sqlite artifact digest mismatch: {artifact}"
                        )
                    blob = artifact.read_bytes()
                    nonce, ciphertext = blob[:12], blob[12:]
                    key, key_version = provider()
                    try:
                        plain = AESGCM(key).decrypt(nonce, ciphertext, None)
                    except Exception as exc:
                        hint = ""
                        manifest_version = item.get("key_version")
                        if manifest_version and str(manifest_version) != str(
                            key_version
                        ):
                            hint = (
                                " (manifest was encrypted under key version "
                                f"{manifest_version!r}; the current provider serves "
                                f"{key_version!r})"
                            )
                        raise RestoreError(
                            f"sqlite artifact failed to decrypt: {exc}{hint}"
                        ) from exc
                    staged_db = staging / "app.db"
                    staged_db.write_bytes(plain)
                    os.chmod(staged_db, 0o600)
                    sqlite_rows = _count_sqlite_rows(staged_db)

                elif kind in ("lancedb", "vault", "draft_room"):
                    src_dir = backup_dir / item_path
                    if not src_dir.exists():
                        raise RestoreError(f"{kind} artifact missing: {src_dir}")
                    expected_files = item.get("files") or {}
                    if not expected_files:
                        # An empty/missing files map would "verify" zero
                        # digests and still report success — that falsifies
                        # the module contract (swarm review F-005) — UNLESS
                        # the producer explicitly recorded an empty tree,
                        # which restores as an empty directory (#727,
                        # T1-28-K-02).
                        if item.get("empty") is True:
                            (staging / Path(item_path)).mkdir(parents=True, exist_ok=True)
                            items_out.append({"name": name, "ok": ok})
                            continue
                        raise RestoreError(
                            f"{kind} artifact {item_path!r} binds no file digests; "
                            "an unverifiable tree is not restorable"
                        )
                    for rel, digest in expected_files.items():
                        candidate = src_dir / rel
                        if not _contained(src_dir, candidate):
                            raise RestoreError(
                                f"manifest file escapes the artifact tree: {rel!r}"
                            )
                        if not candidate.exists():
                            raise RestoreError(
                                f"manifest file missing from backup set: {rel}"
                            )
                        if _sha256_file(candidate) != digest:
                            raise RestoreError(
                                f"artifact digest mismatch: {item_path}/{rel}"
                            )
                    staged_dir = staging / Path(item_path)
                    staged_dir.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(src_dir, staged_dir)
                    _restrict_permissions(staged_dir)

                else:
                    raise RestoreError(f"unknown manifest item kind {kind!r}")

                items_out.append({"name": name, "ok": ok})

            if not items_out:
                raise RestoreError("manifest binds no items")

            # Generation binding against the STAGED database, before any
            # destination mutation.
            _verify_generation_binding(manifest, staging)

            # Unmanaged destination content is copied into staging while
            # the destination is still untouched; stale SQLite sidecars
            # are excluded so they cannot replay over the restored bytes.
            if dest_existed:
                _copy_unmanaged_into_staging(dest_root, staging, roots)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        # --- Swap (aside is IMMUTABLE from here until commit) ---
        aside = dest_root.parent / (
            f"{dest_root.name}.pre-restore-{secrets.token_hex(4)}"
        )
        journal = _write_journal(
            dest_root,
            {
                "phase": "journal-written",
                "dest": str(dest_root),
                "staging": str(staging),
                "aside": str(aside),
                "dest_existed": dest_existed,
            },
        )
        aside_created = False
        try:
            if dest_existed:
                os.rename(dest_root, aside)
                aside_created = True
                # Record the phase AFTER the rename (the disk, not the
                # flag, is the truth a crash leaves behind) so the
                # documented `aside-created` recovery steps match what
                # actually happened (#728 review finding).
                _update_journal_phase(journal, "aside-created")
            try:
                os.rename(staging, dest_root)
            except BaseException:
                if aside_created:
                    # Roll the first rename back; if THIS fails the state
                    # is uncertain and the journal + lock survive (below).
                    os.rename(aside, dest_root)
                    aside_created = False
                raise
        except BaseException as exc:
            # Filesystem truth, not the flag: an async exception can land
            # between the rename and the flag store, so consult the aside
            # directory itself before deciding the outcome is confirmed.
            if aside.exists():
                # The original destination sits at the aside path and the
                # destination path does not hold the pre-state: the
                # outcome is UNCERTAIN — keep the journal AND the lock,
                # and name every directory plus the manual steps.
                raise RestoreError(
                    "restore failed and the destination was left moved "
                    "aside; recovery is UNCERTAIN. Journal kept at "
                    f"{journal}. Original state: {aside}. Staged state: "
                    f"{staging}. Complete the recovery manually per "
                    "docs/operations.md (Restore crash recovery). "
                    f"Original error: {exc}"
                ) from exc
            # Destination is back to (or never left) its pre-restore
            # state: the outcome is confirmed, so the journal goes.
            if journal is not None:
                journal.unlink(missing_ok=True)
                journal = None
            raise
        try:
            _update_journal_phase(journal, "commit")

            # --- Commit window: LanceDB persistence only ---
            tables: List[object] = []
            persistence_error: Optional[BaseException] = None
            try:
                for item in manifest.get("items", []):
                    if item.get("kind") != "lancedb":
                        continue
                    for table_entry in item.get("tables", []):
                        table_name = table_entry["table"]
                        tag = table_entry["tag"]
                        if lancedb_factory is None:
                            continue
                        table = lancedb_factory(table_name)
                        tables.append(table)
                        table.checkout(tag)
                        # Persist the checkout so a FRESH connection serves
                        # the tagged generation (#728 AC5): real lancedb
                        # tables expose restore(); duck-typed fakes without
                        # it keep the checkout-only contract the frozen
                        # fakes pin.
                        restore_fn = getattr(table, "restore", None)
                        if restore_fn is not None:
                            restore_fn()
            except BaseException as exc:  # noqa: BLE001 — ladder decides
                persistence_error = exc

            if persistence_error is not None:
                failed = dest_root.parent / (
                    f"{dest_root.name}.failed-{secrets.token_hex(4)}"
                )
                # Rollback ladder: drop table refs first (lancedb 0.39 has
                # no close(); Rust handles release on drop), then discard
                # the re-derivable restored tree, then put the immutable
                # pre-state back. The journal survives only on UNCERTAIN
                # outcomes; a confirmed rollback deletes it.
                tables.clear()
                gc.collect()
                try:
                    os.rename(dest_root, failed)
                except OSError:
                    shutil.rmtree(dest_root, ignore_errors=True)
                if dest_root.exists():
                    raise RestoreError(
                        "LanceDB persistence failed and the restored "
                        "destination could not be cleared; recovery is "
                        "UNCERTAIN. Journal kept at "
                        f"{journal}. Original state: {aside}. Restored "
                        f"state: {dest_root}. Original error: "
                        f"{persistence_error}"
                    ) from persistence_error
                if dest_existed:
                    try:
                        os.rename(aside, dest_root)
                    except OSError:
                        raise RestoreError(
                            "LanceDB persistence failed and the "
                            "pre-restore directory could not be moved "
                            "back; recovery is UNCERTAIN. Journal kept at "
                            f"{journal}. Original state: {aside}. "
                            f"Original error: {persistence_error}"
                        ) from persistence_error
                # Confirmed rollback (destination absent when it started
                # absent, or the immutable aside moved back).
                journal.unlink(missing_ok=True)
                journal = None
                logger.warning(
                    "restore rolled back after a LanceDB persistence "
                    "failure (%s); the discarded restored copy is kept at "
                    "%s",
                    persistence_error,
                    failed,
                )
                raise RestoreError(
                    "restore rolled back to the pre-restore state after a "
                    f"LanceDB persistence failure: {persistence_error}"
                ) from persistence_error

            # --- Committed ---
            journal.unlink()
            journal = None
            if dest_existed:
                try:
                    shutil.rmtree(aside)
                except OSError as exc:
                    logger.warning(
                        "restore succeeded but the pre-restore directory "
                        "could not be removed: %s (kept at %s)",
                        exc,
                        aside,
                    )
        finally:
            if journal is not None:
                # An unexpected exit path with the journal still present:
                # keep it (recovery evidence) rather than delete it — and
                # keep the lock FILE too: the destination's state is
                # uncertain and a concurrent restore must not proceed
                # (#728 plan 5k step 5).
                logger.warning(
                    "restore exited with the crash journal still present: %s "
                    "(lock retained: %s)",
                    journal,
                    lock.path,
                )
                lock.retain()
            else:
                lock.release()
    # (outer lock guard) every earlier failure path releases here too;
    # release() is idempotent; an uncertain outcome retains the lock FILE
    # (handle closed) instead.
    finally:
        if journal is None:
            lock.release()
        else:
            lock.retain()

    return {
        "verified": verified and all(entry["ok"] for entry in items_out),
        "items": items_out,
        "sqlite_rows": sqlite_rows,
    }


def _verify_generation_binding(manifest: dict, dest_root: Path) -> None:
    """Cross-check the C1 authoritative generation (#518 G6).

    When a lancedb manifest item carries a ``generation`` binding (the row
    the backup read FROM THE SNAPSHOT's migration_journal — the LATEST one
    at snapshot time), the restored SQLite's LATEST index-generation row
    must be that same id. Comparing against MAX(id) (PR #595 review F-003)
    is what makes the check non-tautological: presence-of-id alone could
    never fail on an honest set, because the restored DB is byte-identical
    to the snapshot the binding came from. A restored DB NEWER than the
    binding (an interleaved-write set that paired a newer SQLite with an
    older vector tree) fails HERE with both ids named. A DB older than the
    binding fails the row lookup below. This remains a structural sanity
    check, not tamper-proofing: a fully doctored set could rewrite both
    sides. Legacy sets without the field restore with a WARNING.

    Runs against the STAGING root before anything is swapped (#728
    AC9/AC21).
    """
    binding = None
    for item in manifest.get("items", []):
        if item.get("kind") == "lancedb":
            binding = item.get("generation")
            break
    if binding is None:
        logger.warning(
            "manifest carries no generation binding (legacy set or fresh "
            "install with no journal row); restore proceeds without the "
            "C1 coherence check"
        )
        return
    raw_id = binding.get("id")
    try:
        bound_id = int(raw_id)
    except (TypeError, ValueError):
        raise RestoreError(
            f"generation binding has a malformed id: {raw_id!r}"
        ) from None
    dest_db = dest_root / "app.db"
    if not dest_db.exists():
        raise RestoreError(
            "generation binding present but the sqlite artifact was not "
            "restored alongside the lancedb tree"
        )
    conn = sqlite3.connect(str(dest_db))
    try:
        row = conn.execute(
            "SELECT MAX(id) FROM migration_journal"
            " WHERE migration_name LIKE 'index_generation:%'"
        ).fetchone()
        bound_row = conn.execute(
            "SELECT id FROM migration_journal WHERE id = ?", (bound_id,)
        ).fetchone()
    finally:
        conn.close()
    if bound_row is None:
        raise RestoreError(
            "generation binding mismatch: the manifest binds "
            f"migration_journal id {bound_id} "
            f"({binding.get('name')!r}) but the restored sqlite does not "
            "contain that row — this set pairs a sqlite snapshot with an "
            "inconsistent vector tree; do not use it"
        )
    latest = int(row[0]) if row and row[0] is not None else None
    if latest is not None and latest > bound_id:
        raise RestoreError(
            "generation binding mismatch: the manifest binds generation "
            f"{bound_id} but the restored sqlite's latest index generation "
            f"is {latest} — this set pairs a NEWER sqlite snapshot with an "
            "OLDER vector tree; do not use it"
        )


def _cli_lancedb_factory(
    dest_root: Path,
) -> Callable[[str], object]:
    """Open the RESTORED lancedb tree lazily and return a table factory.

    The operator CLI must actually perform the documented tag checkout
    (swarm review NEW-LANCEDB-UNWIRED): the module docstring,
    docs/operations.md and the admin guide all promise
    ``checkout(tag)`` on restore, which never ran when main() omitted the
    factory. Connection is deferred to the first call so it happens AFTER
    the lancedb tree has been restored into ``dest_root`` (with the staged
    design, that is after the swap).
    """
    import lancedb

    state: dict = {}

    def factory(name: str):
        if "db" not in state:
            state["db"] = lancedb.connect(str(dest_root / "lancedb"))
        return state["db"].open_table(name)

    return factory


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Restore and verify a backup set (see scripts/backup_set.py)"
    )
    parser.add_argument("backup_dir", type=Path, help="Backup set directory")
    parser.add_argument(
        "--dest",
        type=Path,
        default=None,
        help="Destination root (defaults to settings.data_dir)",
    )
    args = parser.parse_args()
    dest = args.dest or Path(settings.data_dir)

    try:
        factory = _cli_lancedb_factory(dest)
    except ImportError:
        raise SystemExit(
            "restore requires the lancedb package to check out tagged "
            "table versions; install the backend requirements first"
        )
    report = restore_backup_set(
        args.backup_dir, dest, lancedb_factory=factory
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
