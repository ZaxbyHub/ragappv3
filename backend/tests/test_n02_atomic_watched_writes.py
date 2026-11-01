"""Arm's-length acceptance checks for issue #786, defect class 1: no write
path may stream bytes directly onto a FINAL path inside a FileWatcher-scanned
vault upload directory.

``FileWatcher._find_new_files`` (file_watcher.py:309, ``rglob("*")`` with
membership checked against the ``files`` table only) enqueues everything the
scan finds that is not yet registered, so a kill mid-write leaves a truncated
file at the final path that the next scan ingests permanently. The two write
paths pinned here are the document upload route
(``app.api.routes.documents._do_upload`` — reservation ``os.open`` with
O_WRONLY at :2554, then ``aiofiles.open(temp_file_path, "wb")`` with
``temp_file_path = file_path`` at :2605) and the startup migration
(``app.services.upload_path.migrate_uploads`` — ``shutil.copy2(f, dest_path)``
at :133 before the size check at :136).

The write recorder mirrors the tracer's verified probe
(``.agents/issue-traces/786-atomic-watched-writes-offloop-compensation/probe/
phase2_repro.py``): it patches ``builtins.open`` and ``io.open``
(``Path.write_bytes`` routes through ``io.open``), ``os.open`` (a path is
recorded only when the flags include O_WRONLY/O_RDWR/O_APPEND — a bare
O_CREAT|O_EXCL create does not count), ``shutil.copy2``/``shutil.copyfile``
destinations, and ``aiofiles.open`` write-mode opens (a plain-def wrapper that
records and returns the ORIGINAL result object so ``async with`` keeps
working). ``os.replace``/``os.rename`` are not byte writes and are not
recorded, so a temp-file-then-rename fix passes while the direct-write base
fails with ``assert True is False``.
"""

import builtins
import io
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies before any app import (the same stub
# block test_documents_auth.py installs — conftest.py covers pyarrow only).
try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    import types

    _unstructured = types.ModuleType("unstructured")
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType("unstructured.partition")
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    _unstructured.chunking = types.ModuleType("unstructured.chunking")
    _unstructured.chunking.__path__ = []
    _unstructured.chunking.title = types.ModuleType("unstructured.chunking.title")
    _unstructured.chunking.title.chunk_by_title = lambda *args, **kwargs: []
    _unstructured.documents = types.ModuleType("unstructured.documents")
    _unstructured.documents.__path__ = []
    _unstructured.documents.elements = types.ModuleType(
        "unstructured.documents.elements"
    )
    _unstructured.documents.elements.Element = type("Element", (), {})
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto
    sys.modules["unstructured.chunking"] = _unstructured.chunking
    sys.modules["unstructured.chunking.title"] = _unstructured.chunking.title
    sys.modules["unstructured.documents"] = _unstructured.documents
    sys.modules["unstructured.documents.elements"] = _unstructured.documents.elements

import aiofiles  # noqa: E402
from test_integration import setup_app_state  # noqa: E402

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.services import upload_path as upload_path_module  # noqa: E402


class WriteRecorder:
    """Record every byte-writing open/copy destination for the patched window.

    Field notes verified at the base commit by the tracer's probe: patching
    both ``builtins.open`` and ``io.open`` is required (``Path.write_bytes``
    routes through ``io.open``); ``os.open`` records only when the flags
    include a write mode (O_CREAT|O_EXCL|O_WRONLY therefore counts, a bare
    O_CREAT|O_EXCL create does not); the ``aiofiles.open`` wrapper must be a
    plain ``def`` returning the ORIGINAL result object so ``async with`` and
    the awaited form both keep working.
    """

    def __init__(self) -> None:
        self.writes: list[tuple[str, str, str]] = []

    @staticmethod
    def _mode_is_write(mode) -> bool:
        m = str(mode or "r")
        return any(f in m for f in ("w", "a", "x")) and "r" not in m

    @contextmanager
    def patch(self):
        real_bopen = builtins.open
        real_io_open = io.open
        real_os_open = os.open
        real_copy2 = shutil.copy2
        real_copyfile = shutil.copyfile
        real_aio_open = aiofiles.open
        rec = self

        def bopen(file, mode="r", *a, **kw):
            if rec._mode_is_write(mode):
                rec.writes.append(("builtins.open", str(file), str(mode)))
            return real_bopen(file, mode, *a, **kw)

        def osopen(path, flags, *a, **kw):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND):
                rec.writes.append(("os.open", str(path), oct(flags)))
            return real_os_open(path, flags, *a, **kw)

        def copy2(src, dst, *a, **kw):
            rec.writes.append(("shutil.copy2", str(dst), "copy2"))
            return real_copy2(src, dst, *a, **kw)

        def copyfile(src, dst, *a, **kw):
            rec.writes.append(("shutil.copyfile", str(dst), "copyfile"))
            return real_copyfile(src, dst, *a, **kw)

        def aioopen(file, mode="r", *a, **kw):
            if rec._mode_is_write(mode):
                rec.writes.append(("aiofiles.open", str(file), str(mode)))
            return real_aio_open(file, mode, *a, **kw)

        builtins.open = bopen
        io.open = bopen
        os.open = osopen
        shutil.copy2 = copy2
        shutil.copyfile = copyfile
        aiofiles.open = aioopen
        try:
            yield rec
        finally:
            builtins.open = real_bopen
            io.open = real_io_open
            os.open = real_os_open
            shutil.copy2 = real_copy2
            shutil.copyfile = real_copyfile
            aiofiles.open = real_aio_open

    def destinations(self) -> set[str]:
        return {path for _kind, path, _mode in self.writes}


def test_document_upload_never_writes_the_final_path_directly():
    """AC1: the upload route must never write bytes to the FINAL vault path.

    Drives a real authenticated upload (the ``setup_app_state`` TestClient
    harness from test_integration.py — the same harness
    test_issue258_e2e_real_worker.py drives a 200 upload with) while the
    write recorder is active, then requires that nothing wrote the uploaded
    file's final path (from its ``files`` row; the vault uploads dir is
    ``settings.vault_uploads_dir(vault_id)``) directly. At the base the
    reservation ``os.open(..., O_CREAT|O_EXCL|O_WRONLY)`` and the
    ``aiofiles.open(temp_file_path, "wb")`` stream both hit the final path,
    so the assertion renders ``assert True is False``.
    """
    from fastapi.testclient import TestClient

    original_data_dir = settings.data_dir
    cleanup = setup_app_state(app)
    try:
        db_path = app.state._test_db_path
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'n02-upload-vault')"
            )
            conn.commit()
        finally:
            conn.close()

        content = b"document bytes for the atomic upload write check " * 4
        client = TestClient(app)
        recorder = WriteRecorder()
        with recorder.patch():
            response = client.post(
                "/api/documents/upload?vault_id=1",
                files={
                    "file": ("n02_upload_probe.txt", BytesIO(content), "text/plain")
                },
            )
        assert response.status_code == 200, response.text
        file_id = response.json()["file_id"]

        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT file_path FROM files WHERE id = ?", (file_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row is not None, "upload must register a files row"
        final_path = Path(row[0])
        assert str(final_path).startswith(
            str(settings.vault_uploads_dir(1))
        ), f"uploaded file must live under the vault uploads dir: {final_path}"

        wrote_final_directly = str(final_path) in recorder.destinations()
        assert wrote_final_directly is False
    finally:
        cleanup()
        settings.data_dir = original_data_dir


def test_migrate_uploads_never_writes_the_destination_directly():
    """AC3: startup upload migration must never write the destination directly.

    Mirrors the b03 DATA_DIR isolation pattern (tmp ``DATA_DIR`` env +
    ``settings.data_dir`` snapshot/restore; see tests/conftest.py's
    ``_b03_data_dir_isolation`` and tests/test_b03_upload_migration_rows.py):
    seeds one legacy file under the legacy flat uploads dir plus a matching
    ``files`` row, patches ``_lookup_vault_id_for_path`` to a known vault,
    and runs ``migrate_uploads(dry_run=False)`` under the write recorder. At
    the base ``shutil.copy2(f, dest_path)`` (upload_path.py:133) streams onto
    the destination before the size check, so the assertion renders
    ``assert True is False``.
    """
    tmp_dir = tempfile.mkdtemp(prefix="n02_migrate_")
    saved_env_data_dir = os.environ.get("DATA_DIR")
    original_data_dir = settings.data_dir
    os.environ["DATA_DIR"] = tmp_dir
    settings.data_dir = Path(tmp_dir)
    try:
        from app.models.database import init_db, run_migrations

        db_path = str(settings.data_dir / "app.db")
        init_db(db_path)
        run_migrations(db_path)

        legacy_dir = settings.uploads_dir
        legacy_dir.mkdir(parents=True, exist_ok=True)
        legacy_path = legacy_dir / "n02_legacy.txt"
        legacy_path.write_bytes(b"legacy upload bytes for the migration check " * 4)

        conn = sqlite3.connect(db_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) "
                "VALUES (1, 'N02 Migration', '')"
            )
            conn.execute(
                "INSERT INTO files (vault_id, file_path, file_name, file_hash, "
                "file_size, status) VALUES (1, ?, 'n02_legacy.txt', "
                "'n02migratehash', ?, 'indexed')",
                (str(legacy_path), legacy_path.stat().st_size),
            )
            conn.commit()
        finally:
            conn.close()

        dest_path = settings.vault_uploads_dir(1) / "n02_legacy.txt"
        recorder = WriteRecorder()
        with patch.object(
            upload_path_module, "_lookup_vault_id_for_path", return_value=1
        ):
            with recorder.patch():
                upload_path_module.migrate_uploads(dry_run=False)

        wrote_final_directly = str(dest_path) in recorder.destinations()
        assert wrote_final_directly is False
    finally:
        if saved_env_data_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = saved_env_data_dir
        settings.data_dir = original_data_dir
        shutil.rmtree(tmp_dir, ignore_errors=True)
