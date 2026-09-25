"""Acceptance checks for issue #660: GET /api/settings/connection infra leak.

FROZEN acceptance checks (they assert the response contract, never a fix
shape):

C1 TestIssue660ViewerRedaction -- viewer and member responses from every
   /api/settings/connection branch must not echo the configured endpoint
   URLs, and the local-mode fallback must not echo the reranker model
   name. Failure marker: ISSUE660-LEAK.

C2 TestIssue660AdminUnchanged -- admin/superadmin responses keep today's
   field sets and values per branch (url/status/ok on success; error
   added for SSRF-blocked / transport-failure / HTTP-failure; local
   branch url/ok/status/model).

C3 TestIssue660DiagnosticsPreserved -- ok and status stay present for
   every role in every branch; error keeps the "SSRF blocked: ",
   "transport failure: " and "embedding inference failed"
   classifications. Must pass on the current tree.

C4 TestIssue660StructuralContract -- the seeded-value family is derived
   from the canonical INFRA_REDACTED_FIELDS tuple in
   app.api.routes.settings; a recursive walker (with a
   non-vacuousness self-check) sweeps the settings endpoints and then
   every parameterless GET route for leaked seeded values. Failure
   marker: ISSUE660-STRUCT.

Network isolation: every HTTP outcome is canned or blocked by
construction. Success branches stub DNS resolution (the REAL URL-safety
guard still executes and passes against the stubbed public answer) and
stub the outbound client class, so responses are canned 200/500; the
SSRF-blocked branch uses a literal loopback IP, which the real guard
blocks without any resolver or socket involvement; the transport-failure
branch stubs DNS to a public IP (guard passes for real) and makes the
canned client raise a host-free connect error. Note the transport
branch cannot use an unresolvable hostname: the real guard raises on
resolver failure first, which lands in the SSRF-blocked branch instead.
All checks are GET-only.
"""

import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from queue import Empty, Queue
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse

import httpx

# Ensure backend importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Shared optional-dep stubs (mirrors test_settings_curator.py).
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

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    import types

    _u = types.ModuleType("unstructured")
    _u.__path__ = []
    _u.partition = types.ModuleType("unstructured.partition")
    _u.partition.__path__ = []
    _u.partition.auto = types.ModuleType("unstructured.partition.auto")
    _u.partition.auto.partition = lambda *a, **k: []
    _u.chunking = types.ModuleType("unstructured.chunking")
    _u.chunking.__path__ = []
    _u.chunking.title = types.ModuleType("unstructured.chunking.title")
    _u.chunking.title.chunk_by_title = lambda *a, **k: []
    _u.documents = types.ModuleType("unstructured.documents")
    _u.documents.__path__ = []
    _u.documents.elements = types.ModuleType("unstructured.documents.elements")
    _u.documents.elements.Element = type("Element", (), {})
    sys.modules["unstructured"] = _u
    sys.modules["unstructured.partition"] = _u.partition
    sys.modules["unstructured.partition.auto"] = _u.partition.auto
    sys.modules["unstructured.chunking"] = _u.chunking
    sys.modules["unstructured.chunking.title"] = _u.chunking.title
    sys.modules["unstructured.documents"] = _u.documents
    sys.modules["unstructured.documents.elements"] = _u.documents.elements

from app.config import settings

# ---------------------------------------------------------------------------
# Seeded, distinctive infrastructure values. Every leak assertion looks for
# these substrings; they cannot appear in a legitimate non-admin response.
# ---------------------------------------------------------------------------

_SEED_EMBED_URL = "http://i660-embed.internal.lan:11434/v1"
_SEED_CHAT_URL = "http://i660-chat.internal.lan:9100"
_SEED_RERANK_URL = "http://i660-rerank.internal.lan:9200"
_SEED_DEAD_URL = "http://i660-dead.internal.lan:9300"
_SEED_LOOPBACK_RERANK_URL = "http://127.0.0.1:64660"
_SEED_RERANK_MODEL = "i660-local-rerank-model-7f3a"

# C1 walks the whole JSON response and flags ANY of these substrings,
# regardless of which branch produced the string.
_C1_URL_NEEDLES = (
    "i660-embed",
    "i660-chat",
    "i660-rerank",
    "i660-dead",
    "127.0.0.1:64660",
)

# Public IP handed out by the stubbed resolver so the REAL URL-safety guard
# can complete and pass (kept public so the guard does not block it).
_DNS_PUBLIC_ANSWER = "93.184.216.34"


class _SimplePool:
    def __init__(self, db_path):
        self.db_path = db_path
        self._pool = Queue(maxsize=5)
        self._lock = threading.Lock()
        self._closed = False

    def get_connection(self):
        if self._closed:
            raise RuntimeError("Pool closed")
        try:
            return self._pool.get_nowait()
        except Empty:
            return self._create_connection()

    def _create_connection(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def release_connection(self, conn):
        if not self._closed:
            try:
                self._pool.put_nowait(conn)
            except Exception:
                conn.close()

    @contextmanager
    def connection(self):
        conn = self.get_connection()
        try:
            yield conn
        finally:
            self.release_connection(conn)

    def close_all(self):
        self._closed = True
        while True:
            try:
                conn = self._pool.get_nowait()
                conn.close()
            except Empty:
                break


# ---------------------------------------------------------------------------
# Recursive walker (shared by C1 and C4).
# ---------------------------------------------------------------------------


def _iter_strings(node, path="root"):
    """Yield (path, string) for every string in a nested dict/list/scalar."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _iter_strings(value, "%s.%s" % (path, key))
    elif isinstance(node, list):
        for idx, value in enumerate(node):
            yield from _iter_strings(value, "%s[%d]" % (path, idx))
    elif isinstance(node, str):
        yield path, node


def _find_seed_hits(payload, needles):
    """Report (path, needle, value) for every string containing a needle."""
    hits = []
    for path, value in _iter_strings(payload):
        for needle in needles:
            if needle in value:
                hits.append((path, needle, value))
    return hits


def _make_fake_dns(host_to_ip):
    """Build a getaddrinfo stand-in that answers mapped hosts and delegates
    everything else to the real resolver (so unrelated lookups still work)."""
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        mapped = host_to_ip.get(host)
        if mapped is None:
            return real_getaddrinfo(host, port, family, type, proto, flags)
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (mapped, port or 80),
            )
        ]

    return fake_getaddrinfo


def _canned_response(status_code):
    response = MagicMock()
    response.status_code = status_code
    return response


# ---------------------------------------------------------------------------
# Shared harness mixin (plain class on purpose: not collected by pytest).
# Mirrors test_settings_curator.TestSettingsExpansion mechanics.
# ---------------------------------------------------------------------------


class _Issue660Harness:
    def setUp(self):
        from fastapi.testclient import TestClient

        from app.api.deps import get_db, get_db_pool
        from app.api.routes.settings import INFRA_REDACTED_FIELDS
        from app.main import app
        from app.services.auth_service import create_access_token, hash_password

        self.app = app
        self.create_token = create_access_token
        self.hash_password = hash_password

        self.tmp = tempfile.mkdtemp()
        self._original_data_dir = settings.data_dir
        self._original_jwt = settings.jwt_secret_key
        self._original_users = settings.users_enabled
        settings.data_dir = Path(self.tmp)
        settings.users_enabled = True
        settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"

        # Snapshot every settings attribute this module can touch: the
        # probe URLs, the local-mode model, plus the whole canonical
        # *_url/*_model redaction family (C4 seeds all of them).
        self._family_fields = [
            field
            for field in INFRA_REDACTED_FIELDS
            if (field.endswith("_url") or field.endswith("_model"))
            and hasattr(settings, field)
        ]
        self._settings_snapshot = {
            field: getattr(settings, field) for field in self._family_fields
        }
        # The SSRF guard must block loopback deterministically: the
        # ALLOW_LOCAL_SERVICES opt-in must be absent for these tests.
        self._original_allow_local = os.environ.pop("ALLOW_LOCAL_SERVICES", None)

        self.db = str(Path(self.tmp) / "app.db")

        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for _, p in list(_pool_cache.items()):
                p.close_all()
            _pool_cache.clear()

        from app.models.database import run_migrations

        run_migrations(self.db)
        self.pool = _SimplePool(self.db)

        def override_db():
            conn = self.pool.get_connection()
            try:
                yield conn
            finally:
                self.pool.release_connection(conn)

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_db_pool] = lambda: self.pool

        # Seed one user per role.
        conn = self.pool.get_connection()
        try:
            conn.execute("DELETE FROM users WHERE id != 0")
            pw = self.hash_password("pass123")
            rows = [
                (1, "admin1", "Admin One", "admin"),
                (2, "member1", "Member One", "member"),
                (3, "viewer1", "Viewer One", "viewer"),
            ]
            for uid, name, full_name, role in rows:
                conn.execute(
                    "INSERT INTO users "
                    "(id, username, hashed_password, full_name, role, is_active) "
                    "VALUES (?, ?, ?, ?, ?, 1)",
                    (uid, name, pw, full_name, role),
                )
            conn.commit()
        finally:
            self.pool.release_connection(conn)

        self.client = TestClient(app)
        self.tokens = {
            "admin": self.create_token(1, "admin1", "admin"),
            "member": self.create_token(2, "member1", "member"),
            "viewer": self.create_token(3, "viewer1", "viewer"),
        }

    def tearDown(self):
        from app.models.database import _pool_cache, _pool_cache_lock

        self.app.dependency_overrides.clear()
        with _pool_cache_lock:
            for _, p in list(_pool_cache.items()):
                p.close_all()
            _pool_cache.clear()
        self.pool.close_all()
        for field, value in self._settings_snapshot.items():
            setattr(settings, field, value)
        settings.data_dir = self._original_data_dir
        settings.jwt_secret_key = self._original_jwt
        settings.users_enabled = self._original_users
        if self._original_allow_local is not None:
            os.environ["ALLOW_LOCAL_SERVICES"] = self._original_allow_local
        else:
            os.environ.pop("ALLOW_LOCAL_SERVICES", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- role/token helpers ------------------------------------------------

    def _token_for(self, role):
        if role == "superadmin":
            # users_enabled=False local mode authenticates the operator
            # secret and maps it to the superadmin role (app.api.deps).
            return settings.admin_secret_token
        return self.tokens[role]

    # -- scenario construction ---------------------------------------------

    def _apply_standard_targets(self):
        settings.ollama_embedding_url = _SEED_EMBED_URL
        settings.ollama_chat_url = _SEED_CHAT_URL
        settings.reranker_url = _SEED_RERANK_URL

    def _public_dns(self, *urls):
        return {urlparse(url).hostname: _DNS_PUBLIC_ANSWER for url in urls}

    def _probe_connection(
        self,
        role,
        *,
        dns_map=None,
        get_result=200,
        post_result=200,
    ):
        """GET /api/settings/connection with canned network layers.

        dns_map: hostname -> public IP answers handed to the REAL URL-safety
        guard (the guard itself is never patched). get_result / post_result:
        a canned HTTP status code, or an Exception instance the canned
        client raises (transport-failure branch).
        """
        client_instance = AsyncMock()
        if isinstance(post_result, Exception):
            client_instance.post = AsyncMock(side_effect=post_result)
        else:
            client_instance.post = AsyncMock(
                return_value=_canned_response(post_result)
            )
        if isinstance(get_result, Exception):
            client_instance.get = AsyncMock(side_effect=get_result)
        else:
            client_instance.get = AsyncMock(
                return_value=_canned_response(get_result)
            )
        with ExitStack() as stack:
            if dns_map:
                stack.enter_context(
                    patch(
                        "app.services.ssrf.socket.getaddrinfo",
                        side_effect=_make_fake_dns(dns_map),
                    )
                )
            async_client_cls = stack.enter_context(
                patch("app.api.routes.settings.httpx.AsyncClient")
            )
            async_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=client_instance
            )
            async_client_cls.return_value.__aexit__ = AsyncMock(
                return_value=None
            )
            response = self.client.get(
                "/api/settings/connection",
                headers={"Authorization": "Bearer %s" % self._token_for(role)},
            )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _scenario_success(self, role):
        self._apply_standard_targets()
        return self._probe_connection(
            role,
            dns_map=self._public_dns(
                _SEED_EMBED_URL, _SEED_CHAT_URL, _SEED_RERANK_URL
            ),
        )

    def _scenario_embed_http_500(self, role):
        self._apply_standard_targets()
        return self._probe_connection(
            role,
            dns_map=self._public_dns(
                _SEED_EMBED_URL, _SEED_CHAT_URL, _SEED_RERANK_URL
            ),
            post_result=500,
        )

    def _scenario_ssrf_blocked(self, role):
        # Literal loopback IP: the real guard blocks it with no resolver and
        # no socket involvement (ALLOW_LOCAL_SERVICES popped in setUp).
        settings.ollama_embedding_url = _SEED_EMBED_URL
        settings.ollama_chat_url = _SEED_CHAT_URL
        settings.reranker_url = _SEED_LOOPBACK_RERANK_URL
        return self._probe_connection(
            role,
            dns_map=self._public_dns(_SEED_EMBED_URL, _SEED_CHAT_URL),
        )

    def _scenario_transport_failure(self, role):
        # The real guard passes the stubbed public answer; the canned client
        # then raises a host-free connect error for every GET target, which
        # lands in the handler's transport-failure branch. (An unresolvable
        # hostname would instead land in the SSRF-blocked branch: the real
        # guard raises before any request is attempted.)
        settings.ollama_embedding_url = _SEED_EMBED_URL
        settings.ollama_chat_url = _SEED_DEAD_URL
        settings.reranker_url = _SEED_RERANK_URL
        canned_connect_error = httpx.ConnectError(
            "canned connect failure for issue 660 checks"
        )
        return self._probe_connection(
            role,
            dns_map=self._public_dns(
                _SEED_EMBED_URL, _SEED_DEAD_URL, _SEED_RERANK_URL
            ),
            get_result=canned_connect_error,
        )

    def _scenario_local_fallback(self, role):
        settings.ollama_embedding_url = _SEED_EMBED_URL
        settings.ollama_chat_url = _SEED_CHAT_URL
        settings.reranker_url = ""
        settings.reranker_model = _SEED_RERANK_MODEL
        return self._probe_connection(
            role,
            dns_map=self._public_dns(_SEED_EMBED_URL, _SEED_CHAT_URL),
        )

    # -- assertion helpers ---------------------------------------------------

    def _assert_no_seed_leak(self, body, role, branch, extra_needles=()):
        needles = list(_C1_URL_NEEDLES) + list(extra_needles)
        hits = _find_seed_hits(body, needles)
        if hits:
            detail = "; ".join(
                "path=%s needle=%r value=%r" % (path, needle, value)
                for path, needle, value in hits
            )
            self.fail(
                "ISSUE660-LEAK: role=%s branch=%s response disclosed seeded "
                "infra value(s): %s" % (role, branch, detail)
            )


# ---------------------------------------------------------------------------
# C1 -- viewer/member redaction across every branch.
# ---------------------------------------------------------------------------


class TestIssue660ViewerRedaction(_Issue660Harness, unittest.TestCase):
    """Non-admin responses must not disclose infra URLs or the local model."""

    def _check_success(self, role):
        body = self._scenario_success(role)
        # Sanity: the success branch really was exercised per target.
        for target in ("embeddings", "chat", "reranker"):
            self.assertTrue(body[target]["ok"], body)
            self.assertEqual(body[target]["status"], 200, body)
        self._assert_no_seed_leak(body, role, "embeddings/chat/reranker success")

    def _check_ssrf_blocked(self, role):
        body = self._scenario_ssrf_blocked(role)
        self.assertTrue(
            body["reranker"]["error"].startswith("SSRF blocked: "), body
        )
        self._assert_no_seed_leak(body, role, "ssrf-blocked reranker")

    def _check_transport_failure(self, role):
        body = self._scenario_transport_failure(role)
        self.assertTrue(
            body["chat"]["error"].startswith("transport failure: "), body
        )
        self._assert_no_seed_leak(body, role, "transport-failure chat/reranker")

    def _check_local_fallback(self, role):
        body = self._scenario_local_fallback(role)
        self.assertEqual(body["reranker"]["status"], "local", body)
        self._assert_no_seed_leak(
            body, role, "local-mode reranker fallback", (_SEED_RERANK_MODEL,)
        )

    def test_viewer_success_branch(self):
        self._check_success("viewer")

    def test_viewer_ssrf_blocked_branch(self):
        self._check_ssrf_blocked("viewer")

    def test_viewer_transport_failure_branch(self):
        self._check_transport_failure("viewer")

    def test_viewer_local_fallback_branch(self):
        self._check_local_fallback("viewer")

    def test_member_success_branch(self):
        self._check_success("member")

    def test_member_ssrf_blocked_branch(self):
        self._check_ssrf_blocked("member")

    def test_member_transport_failure_branch(self):
        self._check_transport_failure("member")

    def test_member_local_fallback_branch(self):
        self._check_local_fallback("member")


# ---------------------------------------------------------------------------
# C2 -- admin/superadmin responses unchanged.
# ---------------------------------------------------------------------------


class TestIssue660AdminUnchanged(_Issue660Harness, unittest.TestCase):
    """Admin callers keep today's field sets and values in every branch."""

    def test_admin_success_branch_urls_and_field_sets(self):
        body = self._scenario_success("admin")
        self.assertEqual(set(body), {"embeddings", "chat", "reranker"})
        self.assertEqual(
            body["embeddings"],
            {"url": _SEED_EMBED_URL, "status": 200, "ok": True},
        )
        self.assertEqual(
            body["chat"],
            {"url": _SEED_CHAT_URL, "status": 200, "ok": True},
        )
        self.assertEqual(
            body["reranker"],
            {"url": _SEED_RERANK_URL, "status": 200, "ok": True},
        )

    def test_admin_embeddings_http_failure_branch(self):
        body = self._scenario_embed_http_500("admin")
        entry = body["embeddings"]
        self.assertEqual(set(entry), {"url", "status", "ok", "error"})
        self.assertEqual(entry["url"], _SEED_EMBED_URL)
        self.assertEqual(entry["status"], 500)
        self.assertFalse(entry["ok"])
        self.assertIn("embedding inference failed", entry["error"])
        self.assertIn("500", entry["error"])

    def test_admin_ssrf_blocked_branch(self):
        body = self._scenario_ssrf_blocked("admin")
        entry = body["reranker"]
        self.assertEqual(set(entry), {"url", "status", "ok", "error"})
        self.assertEqual(entry["url"], _SEED_LOOPBACK_RERANK_URL)
        self.assertIsNone(entry["status"])
        self.assertFalse(entry["ok"])
        self.assertTrue(entry["error"].startswith("SSRF blocked: "))
        # The other targets still report their full shapes.
        self.assertEqual(
            body["embeddings"],
            {"url": _SEED_EMBED_URL, "status": 200, "ok": True},
        )

    def test_admin_transport_failure_branch(self):
        body = self._scenario_transport_failure("admin")
        for target, url in (
            ("chat", _SEED_DEAD_URL),
            ("reranker", _SEED_RERANK_URL),
        ):
            entry = body[target]
            self.assertEqual(set(entry), {"url", "status", "ok", "error"})
            self.assertEqual(entry["url"], url)
            self.assertIsNone(entry["status"])
            self.assertFalse(entry["ok"])
            self.assertTrue(entry["error"].startswith("transport failure: "))

    def test_admin_local_fallback_branch(self):
        body = self._scenario_local_fallback("admin")
        self.assertEqual(
            body["reranker"],
            {
                "url": "local (sentence-transformers)",
                "ok": True,
                "status": "local",
                "model": _SEED_RERANK_MODEL,
            },
        )

    def test_superadmin_local_mode_sees_full_urls(self):
        settings.users_enabled = False
        body = self._scenario_success("superadmin")
        self.assertEqual(body["embeddings"]["url"], _SEED_EMBED_URL)
        self.assertEqual(body["chat"]["url"], _SEED_CHAT_URL)
        self.assertEqual(body["reranker"]["url"], _SEED_RERANK_URL)


# ---------------------------------------------------------------------------
# C3 -- diagnostics fields preserved for every role.
# ---------------------------------------------------------------------------


class TestIssue660DiagnosticsPreserved(_Issue660Harness, unittest.TestCase):
    """ok/status stay present in every branch; error classifications hold."""

    def _run_diagnostics(self, role):
        with self.subTest(role=role, branch="success"):
            body = self._scenario_success(role)
            for target in ("embeddings", "chat", "reranker"):
                self.assertIn("ok", body[target])
                self.assertIn("status", body[target])
                self.assertTrue(body[target]["ok"])
                self.assertEqual(body[target]["status"], 200)
        with self.subTest(role=role, branch="embeddings-http-failure"):
            body = self._scenario_embed_http_500(role)
            self.assertFalse(body["embeddings"]["ok"])
            self.assertEqual(body["embeddings"]["status"], 500)
            self.assertIn(
                "embedding inference failed", body["embeddings"]["error"]
            )
            # Non-embedding targets keep their diagnostics.
            self.assertTrue(body["chat"]["ok"])
            self.assertTrue(body["reranker"]["ok"])
        with self.subTest(role=role, branch="ssrf-blocked"):
            body = self._scenario_ssrf_blocked(role)
            entry = body["reranker"]
            self.assertIn("ok", entry)
            self.assertIn("status", entry)
            self.assertFalse(entry["ok"])
            self.assertIsNone(entry["status"])
            self.assertTrue(entry["error"].startswith("SSRF blocked: "))
            self.assertTrue(body["embeddings"]["ok"])
            self.assertTrue(body["chat"]["ok"])
        with self.subTest(role=role, branch="transport-failure"):
            body = self._scenario_transport_failure(role)
            for target in ("chat", "reranker"):
                entry = body[target]
                self.assertIn("ok", entry)
                self.assertIn("status", entry)
                self.assertFalse(entry["ok"])
                self.assertIsNone(entry["status"])
                self.assertTrue(
                    entry["error"].startswith("transport failure: ")
                )
            self.assertTrue(body["embeddings"]["ok"])
        with self.subTest(role=role, branch="local-fallback"):
            body = self._scenario_local_fallback(role)
            entry = body["reranker"]
            self.assertIn("ok", entry)
            self.assertIn("status", entry)
            self.assertTrue(entry["ok"])
            self.assertEqual(entry["status"], "local")

    def test_viewer_diagnostics_preserved(self):
        self._run_diagnostics("viewer")

    def test_member_diagnostics_preserved(self):
        self._run_diagnostics("member")

    def test_admin_diagnostics_preserved(self):
        self._run_diagnostics("admin")


# ---------------------------------------------------------------------------
# C4 -- structural contract: canonical family + recursive sweep.
# ---------------------------------------------------------------------------

# Route-path fragments excluded from the full-app GET sweep, each with its
# justification. Kept deliberately small and honest: as of this writing no
# parameterless GET path in the OpenAPI schema matches any fragment, so the
# exclusions below guard future route additions, not current ones.
_SWEEP_SKIP_FRAGMENTS = (
    # Server-sent-event endpoints never return a finite JSON body to walk.
    "stream",
    # WebSocket handshake routes are not GET-JSON contracts.
    "ws",
    # Binary/file payloads are not JSON-walkable.
    "download",
    # Bulk exports can mutate job state on the deployment being probed.
    "export",
)


class TestIssue660StructuralContract(_Issue660Harness, unittest.TestCase):
    """The seeded family comes from INFRA_REDACTED_FIELDS; nothing leaks."""

    def setUp(self):
        super().setUp()
        # Seed a distinctive value for every canonical *_url / *_model
        # redaction field that exists as an attribute on settings.
        self.family = {}
        for field in self._family_fields:
            slug = field.replace("_", "-")
            if field.endswith("_url"):
                value = "http://i660-%s.internal.lan:6601" % slug
            else:
                value = "i660-model-%s" % slug
            self.family[field] = value
            setattr(settings, field, value)
        self.needles = sorted(set(self.family.values()))

    def test_c4a_family_built_from_canonical_redacted_set(self):
        """The seeded family is exactly the url/model subset of the canonical
        INFRA_REDACTED_FIELDS tuple, every value distinctive."""
        from app.api.routes.settings import INFRA_REDACTED_FIELDS

        canonical = {
            field
            for field in INFRA_REDACTED_FIELDS
            if field.endswith("_url") or field.endswith("_model")
        }
        self.assertEqual(set(self.family), canonical & set(self.family))
        self.assertTrue(self.family)
        self.assertIn("ollama_embedding_url", self.family)
        self.assertIn("reranker_model", self.family)
        for field, value in self.family.items():
            self.assertTrue(hasattr(settings, field), field)
            self.assertEqual(getattr(settings, field), value)
            self.assertIn("i660-", value)

    def test_c4c_walker_flags_synthetic_leak(self):
        """Non-vacuousness: the walker must flag a known-leaky payload."""
        payload = {
            "url": self.family["ollama_embedding_url"],
            "nested": {"m": [self.family["ollama_chat_url"]]},
        }
        hits = _find_seed_hits(payload, self.needles)
        paths = {path for path, _needle, _value in hits}
        values = {value for _path, _needle, value in hits}
        self.assertEqual(paths, {"root.url", "root.nested.m[0]"})
        self.assertEqual(
            values,
            {self.family["ollama_embedding_url"], self.family["ollama_chat_url"]},
        )

    def test_c4d_real_view_settings_endpoints_clean(self):
        headers = {"Authorization": "Bearer %s" % self.tokens["viewer"]}
        for path in ("/api/settings", "/api/settings/", "/api/settings/connection"):
            response = self.client.get(path, headers=headers)
            self.assertEqual(response.status_code, 200, (path, response.text))
            hits = _find_seed_hits(response.json(), self.needles)
            if hits:
                detail = "; ".join(
                    "%s path=%s value=%r" % (path, p, v) for p, _n, v in hits
                )
                self.fail("ISSUE660-STRUCT: seeded infra value leaked: %s" % detail)

    def test_c4e_full_app_get_sweep_clean(self):
        headers = {"Authorization": "Bearer %s" % self.tokens["viewer"]}
        schema = self.app.openapi()
        skipped = []
        probed = 0
        hits = []
        for path in sorted(schema.get("paths", {})):
            operations = schema["paths"][path]
            if "get" not in operations or "{" in path:
                continue
            skip_reason = None
            for fragment in _SWEEP_SKIP_FRAGMENTS:
                if fragment in path:
                    skip_reason = "allowlist fragment %r" % fragment
                    break
            if skip_reason is not None:
                skipped.append((path, skip_reason))
                continue
            try:
                response = self.client.get(path, headers=headers)
            except Exception as exc:
                skipped.append(
                    (path, "request raised %s" % type(exc).__name__)
                )
                continue
            if response.status_code != 200:
                skipped.append((path, "http %d" % response.status_code))
                continue
            try:
                payload = response.json()
            except ValueError:
                skipped.append((path, "non-json body"))
                continue
            probed += 1
            for hit_path, _needle, hit_value in _find_seed_hits(
                payload, self.needles
            ):
                hits.append((path, hit_path, hit_value))
        print("ISSUE660 sweep: walked %d GET routes" % probed)
        for path, reason in skipped:
            print("ISSUE660 sweep skipped %s (%s)" % (path, reason))
        if hits:
            detail = "; ".join(
                "route %s path %s value %r" % (p, hp, v) for p, hp, v in hits
            )
            self.fail(
                "ISSUE660-STRUCT: seeded infra value leaked on GET route(s): "
                "%s" % detail
            )


if __name__ == "__main__":
    unittest.main()
