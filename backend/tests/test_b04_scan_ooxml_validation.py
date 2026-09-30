"""B04 acceptance check (issue #693) — scan must validate OOXML before enqueue.

Frozen spec for the fix (RED at base dc894f49).

``test_scan_does_not_enqueue_invalid_ooxml`` (AC5): ``FileWatcher.scan_once``
enqueues every unseen file with no content validation, while the OOXML
member check (``_validate_ooxml_member``) lives only in the upload route —
so a corrupt .docx dropped into a vault uploads directory (bypassing the
upload API) is happily enqueued and later blows up in the processor. The
scan path must apply the same validation gate. RED evidence: the invalid
container is enqueued exactly once, i.e. ``assert 1 == 0``.
"""

from __future__ import annotations

import io
import sqlite3
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch


async def test_scan_does_not_enqueue_invalid_ooxml(tmp_path: Path) -> None:
    """AC5: an invalid OOXML container must not be enqueued by the scanner.

    ``bad.docx`` is a real ZIP whose only member is ``junk.txt`` — the shape
    the upload route's ``_validate_ooxml_member`` rejects (no
    ``[Content_Types].xml`` / word processing members). A scan over the
    vault uploads directory must not enqueue it.
    """
    from app.config import settings
    from app.models.database import SQLiteConnectionPool, init_db
    from app.services.file_watcher import FileWatcher

    db_path = str(tmp_path / "app.db")
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO vaults (name) VALUES ('v')")
    conn.commit()
    vid = int(conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()[0])
    conn.close()

    processor = AsyncMock()
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        with patch.object(settings, "data_dir", tmp_path):
            uploads = settings.vault_uploads_dir(vid)
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("junk.txt", "not an ooxml member")
            (uploads / "bad.docx").write_bytes(buf.getvalue())

            fw = FileWatcher(processor=processor, pool=pool)
            await fw.scan_once()
    finally:
        pool.close_all()

    assert processor.enqueue.await_count == 0
