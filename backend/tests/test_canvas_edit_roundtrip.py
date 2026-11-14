"""Issue #1104 AC2 — canvas edit round-trip fidelity under the boundary fix.

Design decision pinned here (the issue's AC2 contract owner):
**tag-without-escaping**. The selected lines and the instruction are
shown to the model byte-verbatim inside their SECURITY BOUNDARY wrapper
tags, and the model's replacement is spliced back byte-verbatim — there
is NO input-side XML escaping and NO output-side unescape/normalization.

Why: the replacement is free text the model produces by rewriting the
selected lines under the instruction. XML-escaping the selection would
show the model an escaped document (``a &amp; b``) and invite it to
answer in the same escaped dialect; un-escaping its output would then
corrupt genuine entity text the document already contained (a literal
``&amp;`` in a code artifact would come back as ``&``). Round-trip
fidelity — bytes in, same-shaped bytes out — is preserved by leaving
both sides verbatim and relying on the SECURITY BOUNDARY clause (AC1,
``test_canvas_edit_prompt_boundary.py``) for injection defense.

RED at the pre-#1104 base for the tagged-region assertions (no regions
exist); the splice-verbatim assertions pin the design against a future
"helpful" escape/unescape pass, which is exactly the corruption vector
this contract exists to prevent.

Harness: the shared CanvasTestBase TestClient pattern from
``test_canvas_artifacts.py`` (temp DB + dependency overrides +
``app.state.llm_client`` recording stub). The fence-stripping behavior
(``_strip_markdown_fences``) is already pinned by
``test_canvas_artifacts.py::test_edit_range_strips_markdown_fences`` and
is deliberately out of scope here — replacements in these tests carry no
fences.

Note: this module deliberately does not manage request-forgery tokens
itself; the shared conftest toggles the test-only bypass for modules like
this one that do not assert that protection.
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies (same guards as the other route suites).
try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    import types

    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

from _db_pool import SimpleConnectionPool
from fastapi.testclient import TestClient

from app.api.deps import get_db
from app.config import settings
from app.main import app
from app.models.database import _pool_cache, _pool_cache_lock, init_db, run_migrations
from app.services.auth_service import compute_client_fingerprint, create_access_token

PW = "unused-test-password-hash"

# XML-special text: every character an escaper would touch.
SPECIALS = 'a & b < c > d "e" \'f\''


class CanvasTestBase(unittest.TestCase):
    """Shared fixture chain: temp DB, app client, users/vault/session seeds.

    Copied from ``test_canvas_artifacts.py`` (issue #509 suite).
    """

    def setUp(self):
        self.client = TestClient(app)
        # Match the fingerprint baked into generated tokens.
        self.client.headers["user-agent"] = ""
        self._temp_dir = tempfile.mkdtemp()

        self._originals = {
            "jwt_secret_key": settings.jwt_secret_key,
            "users_enabled": settings.users_enabled,
            "data_dir": settings.data_dir,
            "canvas_enabled": settings.canvas_enabled,
            "canvas_max_artifact_kb": settings.canvas_max_artifact_kb,
        }
        settings.data_dir = Path(self._temp_dir)
        settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"
        settings.users_enabled = True
        settings.canvas_enabled = True
        settings.canvas_max_artifact_kb = 512

        self._db_path = str(Path(self._temp_dir) / "app.db")

        self._reset_shared_pool_cache()
        init_db(self._db_path)
        run_migrations(self._db_path)
        self._connection_pool = SimpleConnectionPool(self._db_path)

        def override_get_db():
            conn = self._connection_pool.get_connection()
            try:
                yield conn
            finally:
                self._connection_pool.release_connection(conn)

        app.dependency_overrides[get_db] = override_get_db

        # Seed users AFTER migrations so the orphan->Default-vault assignment
        # does not grant unexpected read rows on vault 1.
        conn = self._connection_pool.get_connection()
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("DELETE FROM vault_members")
            conn.execute("DELETE FROM canvas_versions")
            conn.execute("DELETE FROM canvas_artifacts")
            conn.execute("DELETE FROM chat_messages")
            conn.execute("DELETE FROM chat_sessions")
            conn.execute("DELETE FROM users WHERE id != 0")
            # 1: superadmin (policy passes everything)
            conn.execute(
                "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (1, "superadmin", PW, "Super Admin", "superadmin"),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def tearDown(self):
        if hasattr(self, "_connection_pool"):
            self._connection_pool.close_all()
        self._reset_shared_pool_cache()
        app.dependency_overrides.pop(get_db, None)
        if hasattr(app.state, "llm_client"):
            delattr(app.state, "llm_client")
        for key, value in self._originals.items():
            setattr(settings, key, value)
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    @staticmethod
    def _reset_shared_pool_cache():
        with _pool_cache_lock:
            for pool in list(_pool_cache.values()):
                pool.close_all()
            _pool_cache.clear()

    # ── seeds ────────────────────────────────────────────────────────────

    def _db(self):
        return self._connection_pool.get_connection()

    def _release(self, conn):
        self._connection_pool.release_connection(conn)

    def _make_session(self, vault_id=1, user_id=1, title="Canvas session"):
        conn = self._db()
        try:
            session_id = conn.execute(
                "INSERT INTO chat_sessions (vault_id, user_id, title) VALUES (?, ?, ?)",
                (vault_id, user_id, title),
            ).lastrowid
            conn.commit()
            return session_id
        finally:
            self._release(conn)

    # ── auth ─────────────────────────────────────────────────────────────

    def _token(self, user_id, username, role):
        return create_access_token(
            user_id, username, role, client_fingerprint=compute_client_fingerprint("")
        )

    def _super_headers(self):
        return {"Authorization": f"Bearer {self._token(1, 'superadmin', 'superadmin')}"}

    # ── canvas helpers ───────────────────────────────────────────────────

    def _create_artifact(self, session_id, content="print('hello')\n", name="solution",
                         kind="code", language="python"):
        payload = {"kind": kind, "name": name, "content": content}
        if language is not None:
            payload["language"] = language
        return self.client.post(
            f"/api/chat/sessions/{session_id}/artifacts",
            json=payload,
            headers=self._super_headers(),
        )

    def _install_recording_llm(self, replacement):
        """Stub llm_client that records the exact messages it was sent."""
        mock = MagicMock()
        mock.chat_completion = AsyncMock(return_value=replacement)
        app.state.llm_client = mock
        return mock

    def _edit(self, uid, instruction, start=2, end=2, base=1):
        response = self.client.post(
            f"/api/canvas/artifacts/{uid}/edit-range",
            json={
                "start_line": start,
                "end_line": end,
                "instruction": instruction,
                "base_version_no": base,
            },
            headers=self._super_headers(),
        )
        assert response.status_code == 200, response.text
        return self._llm.chat_completion.await_args.args[0]

    def _current_content(self, uid):
        listing = self.client.get(
            f"/api/canvas/artifacts/{uid}/versions", headers=self._super_headers()
        )
        assert listing.status_code == 200, listing.text
        versions = listing.json()["versions"]
        latest = max(v["version_no"] for v in versions)
        export = self.client.get(
            f"/api/canvas/artifacts/{uid}/export", headers=self._super_headers()
        )
        assert export.status_code == 200, export.text
        return export.json()["content"], latest


class TestCanvasEditRoundtrip(CanvasTestBase):
    """AC2: tag-without-escaping round-trip contract."""

    _llm: MagicMock | None = None

    def setUp(self):
        super().setUp()
        session_id = self._make_session()
        self._uid = self._create_artifact(
            session_id, content=f"alpha\n{SPECIALS}\ngamma"
        ).json()["artifact_uid"]

    def test_selected_lines_shown_verbatim_not_escaped(self):
        """XML-special selection bytes reach the model unchanged."""
        self._llm = self._install_recording_llm("edited")
        messages = self._edit(self._uid, "tighten the sentence")
        user = messages[1]["content"]
        assert f"<selected_lines>{SPECIALS}</selected_lines>" in user, (
            "the selected lines must be shown to the model byte-verbatim "
            "inside the <selected_lines> region — XML-escaping them would "
            "corrupt the replacement text the model must return (#1104 AC2)"
        )

    def test_instruction_shown_verbatim_not_escaped(self):
        """XML-special instruction bytes reach the model unchanged."""
        self._llm = self._install_recording_llm("edited")
        instruction = f'quote {SPECIALS} exactly'
        messages = self._edit(self._uid, instruction)
        user = messages[1]["content"]
        assert f"<instruction>{instruction}</instruction>" in user, (
            "the instruction must reach the model byte-verbatim inside the "
            "<instruction> region — an escaped instruction teaches the "
            "model an escaped output dialect that would splice entities "
            "into the artifact (#1104 AC2)"
        )

    def test_replacement_spliced_verbatim_no_output_normalization(self):
        """Entity-looking replacement text is NOT unescaped on splice.

        Pins the output side of the tag-without-escaping design: the model
        may legitimately return literal ``&amp;``/``&lt;`` sequences (e.g.
        HTML source edits); an output-side unescape would rewrite them and
        corrupt the artifact.
        """
        replacement = 'x = "&amp;" <b>not-bold</b>'
        self._llm = self._install_recording_llm(replacement)
        self._edit(self._uid, "rewrite line 2")
        content, version_no = self._current_content(self._uid)
        assert version_no == 2, "the model_edit version must have been appended"
        assert replacement in content, (
            "the replacement must be spliced byte-verbatim — output-side "
            "unescape/normalization would corrupt genuine entity text"
        )

    def test_end_to_end_roundtrip_with_markup_line(self):
        """Full loop: verbatim in, verbatim out, other lines untouched."""
        replacement = 'A & B < C > D "E"'
        self._llm = self._install_recording_llm(replacement)
        messages = self._edit(self._uid, "uppercase it")
        user = messages[1]["content"]
        # Input side: the model saw the original bytes, not entities.
        assert f"<selected_lines>{SPECIALS}</selected_lines>" in user
        # Output side: the replacement bytes land in the artifact as-is.
        content, _ = self._current_content(self._uid)
        assert content == f"alpha\n{replacement}\ngamma", (
            "the spliced version must equal the untouched lines plus the "
            "model's replacement, byte for byte"
        )


if __name__ == "__main__":
    unittest.main()
