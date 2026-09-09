"""Issue #513 AC22 (CONFIG-002): per-file enrichment override must read back.

Real API round-trip via FastAPI TestClient with dependency overrides (modeled
on backend/tests/test_enrichment_toggle.py): PUT an explicit true/false
enrichment override on a file, then GET the document detail AND the document
list — both must return the explicit persisted value; clearing (null) must
read back as null. The PUT write path and response mapper already include the
column; the defect is the GET projections dropping it.

DISCRIMINATING: at the pre-fix commit the detail and list SELECTs omit
``enrichment_enabled``, so the GETs report null and this script prints
``C22 CHECK: FAIL: ...`` and exits 1.
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # repo root (this file: ROOT/backend/tests/issue513_checks/)
sys.path.insert(0, str(ROOT / "backend"))

_TMP = tempfile.mkdtemp(prefix="issue513_c22_")
os.environ["ADMIN_SECRET_TOKEN"] = "test-secret"
os.environ["USERS_ENABLED"] = "false"
os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only-min-32-chars"
os.environ["REDIS_URL"] = ""
os.environ["DATA_DIR"] = _TMP


def _build_client(tmp: Path):
    import sqlite3

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    db_path = str(Path(tmp) / "app.db")

    from app.models.database import _pool_cache, _pool_cache_lock, get_pool, init_db

    with _pool_cache_lock:
        for pool in list(_pool_cache.values()):
            pool.close_all()
        _pool_cache.clear()
    init_db(db_path)
    pool = get_pool(db_path)

    from app.config import settings

    saved_data_dir = settings.data_dir
    settings.data_dir = Path(tmp)

    app = FastAPI()
    from app.api.deps import (
        get_current_active_user,
        get_current_user_or_service_account,
        get_db,
    )
    from app.api.routes.documents import router as documents_router
    from app.security import csrf_protect

    app.include_router(documents_router, prefix="/api")

    def override_get_db():
        conn = pool.get_connection()
        try:
            yield conn
        finally:
            pool.release_connection(conn)

    superadmin = {
        "id": 1,
        "username": "superadmin",
        "full_name": "Super Admin",
        "role": "superadmin",
        "is_active": True,
        "must_change_password": False,
    }

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
    app.dependency_overrides[get_current_active_user] = lambda: superadmin
    app.dependency_overrides[get_current_user_or_service_account] = lambda: superadmin

    # Seed one vault + one indexed file row directly.
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO vaults (name, description) VALUES ('c22vault', 'check vault')"
    )
    vault_id = conn.execute("SELECT MAX(id) FROM vaults").fetchone()[0]
    cur = conn.execute(
        """INSERT INTO files
           (vault_id, file_path, file_name, file_hash, file_size, file_type, status)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (vault_id, str(tmp / "doc.txt"), "doc.txt", "c22hash", 42, "text/plain", "indexed"),
    )
    conn.commit()
    file_id = cur.lastrowid
    conn.close()

    return TestClient(app), pool, vault_id, file_id, saved_data_dir, settings


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="c22_api_"))
    client, pool, vault_id, file_id, saved_data_dir, settings = _build_client(tmp)
    try:
        def put_override(value):
            return client.put(
                f"/api/documents/{file_id}/enrichment-toggle",
                json={"enabled": value},
            )

        def get_detail():
            return client.get(f"/api/documents/{file_id}")

        def get_list_item():
            resp = client.get(f"/api/documents/?vault_id={vault_id}")
            if resp.status_code != 200:
                return None, resp
            docs = resp.json().get("documents", [])
            for d in docs:
                if d.get("id") == file_id:
                    return d, resp
            return None, resp

        for explicit in (True, False):
            resp = put_override(explicit)
            if resp.status_code != 200:
                print(
                    f"C22 CHECK: FAIL: PUT enabled={explicit} returned "
                    f"{resp.status_code}: {resp.text[:200]}"
                )
                return 1
            if resp.json().get("enrichment_enabled") is not explicit:
                print(
                    f"C22 CHECK: FAIL: PUT enabled={explicit} response lost the "
                    f"override ({resp.json().get('enrichment_enabled')!r})"
                )
                return 1

            dresp = get_detail()
            if dresp.status_code != 200:
                print(
                    f"C22 CHECK: FAIL: GET detail returned {dresp.status_code}: "
                    f"{dresp.text[:200]}"
                )
                return 1
            if dresp.json().get("enrichment_enabled") is not explicit:
                print(
                    f"C22 CHECK: FAIL: after PUT enabled={explicit}, GET detail "
                    f"returned enrichment_enabled="
                    f"{dresp.json().get('enrichment_enabled')!r} — projection "
                    "drops the persisted override column"
                )
                return 1

            item, lresp = get_list_item()
            if item is None:
                print(
                    f"C22 CHECK: FAIL: GET list did not include the file "
                    f"(status={lresp.status_code})"
                )
                return 1
            if item.get("enrichment_enabled") is not explicit:
                print(
                    f"C22 CHECK: FAIL: after PUT enabled={explicit}, GET list "
                    f"returned enrichment_enabled={item.get('enrichment_enabled')!r} "
                    "— projection drops the persisted override column"
                )
                return 1

        resp = put_override(None)
        if resp.status_code != 200:
            print(
                f"C22 CHECK: FAIL: PUT enabled=null returned {resp.status_code}: "
                f"{resp.text[:200]}"
            )
            return 1
        dresp = get_detail()
        if dresp.status_code != 200 or dresp.json().get("enrichment_enabled") is not None:
            print(
                "C22 CHECK: FAIL: clearing the override did not read back as null "
                f"(detail enrichment_enabled={dresp.json().get('enrichment_enabled')!r})"
            )
            return 1

        print("C22 CHECK: PASS")
        return 0
    finally:
        settings.data_dir = saved_data_dir
        try:
            pool.close_all()
        except Exception:  # noqa: BLE001 - cleanup only
            pass


def test_c22_enrichment_override_readback():
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
