"""Issue #513 acceptance check C4 (AC4 / INGEST-005).

A single failing pool checkout (RuntimeError, exactly once) at an ingestion
entry point must not permanently consume the shared SQLite write permit:
the next operation on the same processor must proceed without blocking.

Discriminating: at the pre-fix tree, ``await self._write_semaphore.acquire()``
precedes ``conn = self.pool.get_connection()`` and the checkout sits outside
the try/finally that releases the permit, so one checkout failure leaks the
permit forever -> FAIL. Post-fix the permit is released on every path -> PASS.
"""

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Repo root = four levels up from this file (backend/tests/issue513_checks/x.py).
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "backend"))


def main() -> int:
    os.environ.setdefault("ADMIN_SECRET_TOKEN", "test-secret")
    os.environ.setdefault("USERS_ENABLED", "false")
    os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-for-testing-only")
    os.environ.setdefault("REDIS_URL", "")

    from app.services.document_processor import DocumentProcessor

    class _ProbeReached(RuntimeError):
        """Raised by the fake connection's execute: proof the operation got
        past the semaphore acquire AND the pool checkout."""

    class _ProbeConnection:
        def execute(self, *_args, **_kwargs):
            raise _ProbeReached("reached connection use (probe)")

        def commit(self):
            pass

        def rollback(self):
            pass

    class _OnceFailingPool:
        """pool.get_connection raises RuntimeError exactly once (first call),
        then hands out a probe connection."""

        def __init__(self):
            self._failed_once = False

        def get_connection(self, *_args, **_kwargs):
            if not self._failed_once:
                self._failed_once = True
                raise RuntimeError("injected one-time pool checkout failure")
            return _ProbeConnection()

        def release_connection(self, *_args, **_kwargs):
            pass

    tmp_dir = tempfile.mkdtemp(prefix="c4_513_")
    try:
        file_path = Path(tmp_dir) / "doc.sql"
        file_path.write_text(
            "CREATE TABLE t0 (id INTEGER);\n", encoding="utf-8"
        )

        async def run_entry_point(name: str) -> tuple[bool, str]:
            sem = asyncio.Semaphore(1)
            pool = _OnceFailingPool()
            proc = DocumentProcessor(pool=pool, write_semaphore=sem)

            # -- Operation 1: the pool checkout fails exactly once. ----------
            first_error = None
            try:
                if name == "process_file":
                    await asyncio.wait_for(
                        proc.process_file(str(file_path), vault_id=1), timeout=5
                    )
                else:
                    await asyncio.wait_for(
                        proc.process_existing_file(
                            file_id=1, file_path=str(file_path), vault_id=1
                        ),
                        timeout=5,
                    )
            except Exception as exc:  # noqa: BLE001 - failure is the scenario
                first_error = exc

            # The failure itself is expected; what matters is the permit.
            if sem.locked():
                return (
                    False,
                    f"{name}: write permit leaked after pool checkout failure "
                    f"(semaphore still held after first op; error was "
                    f"{type(first_error).__name__})",
                )

            # -- Operation 2: checkout now succeeds; must not block. ---------
            try:
                if name == "process_file":
                    await asyncio.wait_for(
                        proc.process_file(str(file_path), vault_id=1), timeout=5
                    )
                else:
                    await asyncio.wait_for(
                        proc.process_existing_file(
                            file_id=1, file_path=str(file_path), vault_id=1
                        ),
                        timeout=5,
                    )
                return (
                    False,
                    f"{name}: second operation unexpectedly completed without "
                    "reaching the probe connection",
                )
            except _ProbeReached:
                # Got past acquire + checkout: the permit was fully released.
                return (True, "")
            except asyncio.TimeoutError:
                return (
                    False,
                    f"{name}: second operation blocked on the write permit "
                    "(acquire timed out; permit leaked by first operation)",
                )
            except Exception as exc:  # noqa: BLE001
                return (
                    False,
                    f"{name}: second operation failed unexpectedly before "
                    f"reaching connection use: {type(exc).__name__}: {exc}",
                )

        results = []
        for entry in ("process_file", "process_existing_file"):
            ok, reason = asyncio.run(run_entry_point(entry))
            results.append((entry, ok, reason))

        failures = [reason for _name, ok, reason in results if not ok]
        if failures:
            print(f"C4 CHECK: FAIL: {'; '.join(failures)}")
            return 1
        print("C4 CHECK: PASS")
        return 0
    except Exception as exc:  # noqa: BLE001 - never crash without a verdict
        print(f"C4 CHECK: FAIL: unexpected error: {type(exc).__name__}: {exc}")
        return 1
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())


def test_c4_write_permit_release():
    assert main() == 0
