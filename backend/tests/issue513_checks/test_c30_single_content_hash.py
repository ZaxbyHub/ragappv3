"""Issue #513 AC30: the async upload path must compute the content hash once.

Mirrors the async upload flow end-to-end at processor level: the route side
of ``_do_upload`` (compute hash -> duplicate check row insert carrying the
hash, exactly as documents.py does), then the worker side —
``DocumentProcessor.process_existing_file`` invoked the way
background_tasks.py enqueues it. ``compute_file_hash`` is counted across both
phases: one upload must trigger exactly ONE full-content hash computation
feeding dedup, storage, and chunk identity. If a fixed implementation accepts
the route-computed hash on the worker call (any of the conventional kwarg
names), it is carried through.

DISCRIMINATING: at the pre-fix commit the worker unconditionally re-derives
the hash (document_processor.py "re-derive file_hash here"), so the count is
2 and this script prints ``C30 CHECK: FAIL: ...`` and exits 1.
"""

import asyncio
import inspect
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # repo root (this file: ROOT/backend/tests/issue513_checks/)
sys.path.insert(0, str(ROOT / "backend"))

_TMP = tempfile.mkdtemp(prefix="issue513_c30_")
os.environ["ADMIN_SECRET_TOKEN"] = "test-secret"
os.environ["USERS_ENABLED"] = "false"
os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only-min-32-chars"
os.environ["REDIS_URL"] = ""
os.environ["DATA_DIR"] = _TMP

HASH_KWARG_NAMES = ("file_hash", "content_hash", "hash")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="c30_flow_"))
    db_path = str(tmp / "app.db")

    from app.models.database import get_pool, init_db

    init_db(db_path)
    pool = get_pool(db_path)

    # Count every full-content hash computation in both namespaces the flow
    # uses: the route imports at call time from app.utils.file_utils, and
    # document_processor binds the name at module import.
    import app.services.document_processor as dp_module
    import app.utils.file_utils as fu_module

    calls = {"n": 0}
    real_hash = fu_module.compute_file_hash

    def counting_hash(path):
        calls["n"] += 1
        return real_hash(path)

    fu_module.compute_file_hash = counting_hash
    dp_module.compute_file_hash = counting_hash

    from app.services.document_processor import DocumentProcessor

    processor = DocumentProcessor(chunk_size_chars=1500, chunk_overlap_chars=100, pool=pool)

    src = tmp / "note_c30.txt"
    src.write_text("Acceptance-check payload for issue 513 AC30.\n" * 4, encoding="utf-8")

    # Seed a vault row so the file row's vault reference is valid.
    conn = pool.get_connection()
    try:
        conn.execute("INSERT INTO vaults (name) VALUES ('c30vault')")
        conn.commit()
        vault_id = conn.execute("SELECT MAX(id) FROM vaults").fetchone()[0]
    finally:
        pool.release_connection(conn)

    # --- Route phase (documents.py _do_upload): hash once, register the row.
    from app.utils.file_utils import compute_file_hash  # patched counting version

    file_hash = compute_file_hash(str(src))
    conn = pool.get_connection()
    try:
        file_id = processor._insert_or_get_file_record(  # noqa: SLF001 - route mirror
            str(src), file_hash, conn, vault_id, "upload", None, None
        )
        conn.commit()
    finally:
        pool.release_connection(conn)

    # --- Worker phase (background_tasks.py): process the registered row.
    kwargs = {"file_id": file_id, "file_path": str(src), "vault_id": vault_id}
    params = inspect.signature(processor.process_existing_file).parameters
    for name in HASH_KWARG_NAMES:
        if name in params:
            kwargs[name] = file_hash  # carry the route-computed hash through
            break
    try:
        asyncio.run(processor.process_existing_file(**kwargs))
    except Exception:
        # Downstream pipeline state (parser/embeddings) is out of scope for
        # the hash-count contract; the hash decision happens before the try
        # block in process_existing_file, so the count is already complete.
        pass

    if calls["n"] != 1:
        print(
            f"C30 CHECK: FAIL: compute_file_hash executed {calls['n']} times for "
            "one async upload (route registration + worker re-derivation); "
            "expected exactly 1 hash feeding dedup, storage, and chunk identity"
        )
        return 1

    print("C30 CHECK: PASS")
    return 0


def test_c30_single_content_hash():
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
