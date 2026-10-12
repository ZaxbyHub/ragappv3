"""Arm's-length acceptance check for issue #786, defect class 1 (AC2): Draft
Room promotion must never write bytes directly onto the FINAL destination
path inside the vault's FileWatcher-scanned upload directory.

The issue names both promotion writers: the input copy
(``app.services.draft_promotion._copy_input_bytes`` — ``shutil.copy2`` onto
the reserved destination at :242, after the O_WRONLY reservation in
``_reserve_destination_path`` at :195) and the revision markdown renderer
(``_write_revision_markdown`` — ``dest.write_bytes`` at :246). A kill mid-copy
leaves a truncated file at the final path that ``FileWatcher._find_new_files``
(file_watcher.py:309) enqueues for permanent ingestion.

The write recorder mirrors the tracer's verified probe (see
``tests/test_n02_atomic_watched_writes.py`` for the shared field notes);
``os.replace``/``os.rename`` are not recorded, so a temp-file-then-rename fix
passes while the direct-write base fails with ``assert True is False``.
"""

import builtins
import io
import os
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import aiofiles  # noqa: E402

from app.config import settings  # noqa: E402
from draft_room.test_draft_promote import DraftPromoteTestBase  # noqa: E402


class WriteRecorder:
    """Record every byte-writing open/copy destination for the patched window.

    Same verified shape as ``tests/test_n02_atomic_watched_writes.py``
    (duplicated rather than imported, per this package's convention — see
    test_draft_promote.py's harness docstring): patches both ``builtins.open``
    and ``io.open`` (``Path.write_bytes`` routes through ``io.open``),
    ``os.open`` only when the flags include a write mode, and
    ``shutil.copy2``/``shutil.copyfile`` destinations, plus a plain-def
    ``aiofiles.open`` wrapper that returns the ORIGINAL result object so
    ``async with`` keeps working.
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


class TestN02AtomicPromotionWrites(DraftPromoteTestBase):
    """AC2: promotion's destination bytes must land via an atomic publish
    (temp path + rename), never by writing the reserved final path itself."""

    def _file_path_for(self, file_id: int) -> str:
        conn = self._connection_pool.get_connection()
        try:
            row = conn.execute(
                "SELECT file_path FROM files WHERE id = ?", (file_id,)
            ).fetchone()
        finally:
            self._connection_pool.release_connection(conn)
        self.assertIsNotNone(row, "promotion must register a files row")
        return str(row["file_path"])

    def test_promote_input_never_writes_the_destination_directly(self):
        content = b"manuscript bytes for the atomic promotion write check " * 4
        draft_id = self._create_draft().json()["id"]
        upload = self._upload_input(draft_id, content=content, filename="manuscript.txt")
        self.assertEqual(upload.status_code, 202, upload.text)
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)

        recorder = WriteRecorder()
        with recorder.patch():
            promoted = self._promote(
                draft_id, source_type="input", source_id=input_id, title="Atomic Input"
            )
        self.assertEqual(promoted.status_code, 202, promoted.text)
        input_dest = Path(self._file_path_for(promoted.json()["file_id"]))
        self.assertTrue(
            str(input_dest).startswith(
                str(settings.vault_uploads_dir(self.READ_VAULT_ID))
            ),
            f"promoted file must live under the vault uploads dir: {input_dest}",
        )

        wrote_dest_directly = str(input_dest) in recorder.destinations()
        assert wrote_dest_directly is False

        # The issue names both promotion writers: drive the REVISION leg in
        # the same test and require the same atomicity for the rendered .md.
        revision = self._create_revision(
            draft_id, content_md="# Atomic Revision\n\nExact revision bytes."
        )
        self.assertEqual(revision.status_code, 201, revision.text)
        revision_id = revision.json()["summary"]["id"]

        md_recorder = WriteRecorder()
        with md_recorder.patch():
            promoted_md = self._promote(
                draft_id,
                source_type="revision",
                source_id=revision_id,
                title="Atomic Revision",
            )
        self.assertEqual(promoted_md.status_code, 202, promoted_md.text)
        md_dest = Path(self._file_path_for(promoted_md.json()["file_id"]))
        self.assertTrue(str(md_dest).endswith(".md"))

        wrote_md_dest_directly = str(md_dest) in md_recorder.destinations()
        assert wrote_md_dest_directly is False
