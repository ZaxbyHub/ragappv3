"""Additive non-admin response-shape contract for GET /settings/connection
(issue #660, follow-up coverage beyond the frozen acceptance-check module).

The frozen module (test_issue660_connection_redaction.py) pins leak absence
and admin preservation. This file pins the exact NON-ADMIN representation the
fix ships: url becomes the non-identifying target key (still a str), the
local-mode ``model`` entry is absent, and error values are the classification
prefix plus the exception type name only -- no configured URL, hostname, IP,
or URL fragment anywhere, including the shipped-default empty-config case
(``ollama_chat_url=""``).

This module is deliberately OUTSIDE the issue-tracer frozen manifest paths so
the frozen check bytes stay identical.
"""

import os
import queue
import shutil
import socket
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse

import httpx
from fastapi.testclient import TestClient

from app.config import settings

Empty = queue.Empty
Queue = queue.Queue

_SEED_EMBED_URL = "http://i660-embed.internal.lan:11434/v1"
_SEED_CHAT_URL = "http://i660-chat.internal.lan:9100"
_SEED_RERANK_URL = "http://i660-rerank.internal.lan:9200"
_SEED_RERANK_MODEL = "i660-local-rerank-model-7f3a"
_DNS_PUBLIC_ANSWER = "93.184.216.34"


def _make_fake_dns(host_to_ip):
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


class _SimplePool:
    def __init__(self, db_path):
        self.db_path = db_path
        self._pool = Queue(maxsize=5)
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

    def close_all(self):
        self._closed = True
        while True:
            try:
                conn = self._pool.get_nowait()
                conn.close()
            except Empty:
                break


class TestIssue660NonAdminShape(unittest.TestCase):
    def setUp(self):
        from app.api.deps import get_db, get_db_pool
        from app.api.routes.settings import INFRA_REDACTED_FIELDS
        from app.main import app
        from app.models.database import _pool_cache, _pool_cache_lock, run_migrations
        from app.services.auth_service import create_access_token, hash_password

        self.app = app
        self.tmp = tempfile.mkdtemp()
        # Cleanup contract (review PRR-F2): every module-global mutation gets
        # its addCleanup registered BEFORE the mutation, so a setUp abort can
        # never poison the settings singleton / dependency_overrides for the
        # rest of the worker (repo precedent: TestSettingsInfraRedaction).
        # addCleanup runs LIFO: overrides clear -> pool close -> globals
        # restore -> tempdir removal.
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._original_data_dir = settings.data_dir
        self._original_jwt = settings.jwt_secret_key
        self._original_users = settings.users_enabled
        self._original_chat_api_key = settings.chat_api_key
        self._original_instant_api_key = settings.instant_api_key
        self.addCleanup(self._restore_globals)
        settings.data_dir = Path(self.tmp)
        settings.users_enabled = True
        settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"

        family = [
            field
            for field in INFRA_REDACTED_FIELDS
            if (field.endswith("_url") or field.endswith("_model"))
            and hasattr(settings, field)
        ]
        self._snapshot = {f: getattr(settings, f) for f in family}
        self._original_allow_local = os.environ.pop("ALLOW_LOCAL_SERVICES", None)

        self.db = str(Path(self.tmp) / "app.db")
        with _pool_cache_lock:
            for _, p in list(_pool_cache.items()):
                p.close_all()
            _pool_cache.clear()
        run_migrations(self.db)
        self.pool = _SimplePool(self.db)
        self.addCleanup(self.pool.close_all)

        def override_db():
            conn = self.pool.get_connection()
            try:
                yield conn
            finally:
                self.pool.release_connection(conn)

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_db_pool] = lambda: self.pool
        self.addCleanup(app.dependency_overrides.clear)

        conn = self.pool.get_connection()
        try:
            conn.execute("DELETE FROM users WHERE id != 0")
            pw = hash_password("pw")
            conn.execute(
                "INSERT INTO users (id, username, hashed_password, full_name,"
                " role, is_active) VALUES (1, 'viewer1', ?, 'V', 'viewer', 1)",
                (pw,),
            )
            conn.execute(
                "INSERT INTO users (id, username, hashed_password, full_name,"
                " role, is_active) VALUES (2, 'admin1', ?, 'A', 'admin', 1)",
                (pw,),
            )
            conn.commit()
        finally:
            self.pool.release_connection(conn)

        self.client = TestClient(app)
        self.viewer_token = create_access_token(1, "viewer1", "viewer")
        self.admin_token = create_access_token(2, "admin1", "admin")

    def _restore_globals(self):
        for field, value in self._snapshot.items():
            setattr(settings, field, value)
        settings.data_dir = self._original_data_dir
        settings.jwt_secret_key = self._original_jwt
        settings.users_enabled = self._original_users
        settings.chat_api_key = self._original_chat_api_key
        settings.instant_api_key = self._original_instant_api_key
        if self._original_allow_local is not None:
            os.environ["ALLOW_LOCAL_SERVICES"] = self._original_allow_local

    # -- scenario helpers ---------------------------------------------------

    def _public_dns(self, *urls):
        return {urlparse(url).hostname: _DNS_PUBLIC_ANSWER for url in urls}

    def _probe(self, *, dns_map=None, get_result=200, post_result=200):
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
                headers={"Authorization": "Bearer %s" % self.viewer_token},
            )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _standard_targets(self):
        settings.ollama_embedding_url = _SEED_EMBED_URL
        settings.ollama_chat_url = _SEED_CHAT_URL
        settings.reranker_url = _SEED_RERANK_URL
        return self._probe(
            dns_map=self._public_dns(
                _SEED_EMBED_URL, _SEED_CHAT_URL, _SEED_RERANK_URL
            )
        )

    def _assert_no_config_echo(self, text):
        for fragment in (
            "i660-",
            "127.0.0.1",
            "internal.lan",
            "203.0.113",
            _SEED_RERANK_MODEL,
        ):
            self.assertNotIn(fragment, text)

    # -- the shape contract --------------------------------------------------

    def test_viewer_url_is_target_key_in_every_probed_branch(self):
        # Success branch.
        settings.ollama_embedding_url = _SEED_EMBED_URL
        settings.ollama_chat_url = _SEED_CHAT_URL
        settings.reranker_url = _SEED_RERANK_URL
        body = self._probe(
            dns_map=self._public_dns(
                _SEED_EMBED_URL, _SEED_CHAT_URL, _SEED_RERANK_URL
            )
        )
        for name in ("embeddings", "chat", "reranker"):
            self.assertEqual(body[name]["url"], name)
            self.assertIsInstance(body[name]["url"], str)

        # SSRF-blocked branch (loopback reranker target).
        settings.reranker_url = "http://127.0.0.1:64660"
        body = self._probe(
            dns_map=self._public_dns(_SEED_EMBED_URL, _SEED_CHAT_URL)
        )
        for name in ("embeddings", "chat", "reranker"):
            self.assertEqual(body[name]["url"], name)

        # Transport-failure branch (canned host-free connect error).
        canned = httpx.ConnectError("canned connect failure")
        body = self._probe(dns_map=self._public_dns(
            _SEED_EMBED_URL, _SEED_CHAT_URL, _SEED_RERANK_URL
        ), get_result=canned)
        for name in ("embeddings", "chat", "reranker"):
            self.assertEqual(body[name]["url"], name)

    def test_viewer_local_fallback_model_absent_url_literal_kept(self):
        settings.ollama_embedding_url = _SEED_EMBED_URL
        settings.ollama_chat_url = _SEED_CHAT_URL
        settings.reranker_url = ""
        settings.reranker_model = _SEED_RERANK_MODEL
        body = self._probe(
            dns_map=self._public_dns(_SEED_EMBED_URL, _SEED_CHAT_URL)
        )
        reranker = body["reranker"]
        self.assertNotIn("model", reranker)
        self.assertEqual(reranker["url"], "local (sentence-transformers)")
        self.assertEqual(reranker["status"], "local")
        self.assertTrue(reranker["ok"])

    def test_viewer_error_is_prefix_plus_type_name_only(self):
        # SSRF branch: prefix verbatim, detail is the exception type name.
        settings.ollama_embedding_url = _SEED_EMBED_URL
        settings.ollama_chat_url = _SEED_CHAT_URL
        settings.reranker_url = "http://127.0.0.1:64660"
        body = self._probe(
            dns_map=self._public_dns(_SEED_EMBED_URL, _SEED_CHAT_URL)
        )
        self.assertEqual(body["reranker"]["error"], "SSRF blocked: URLBlocked")
        self.assertTrue(body["reranker"]["error"].startswith("SSRF blocked: "))

        # Transport branch: detail is the exception type name, no host/IP.
        canned = httpx.ConnectError("canned connect failure")
        settings.reranker_url = _SEED_RERANK_URL
        body = self._probe(
            dns_map=self._public_dns(
                _SEED_EMBED_URL, _SEED_CHAT_URL, _SEED_RERANK_URL
            ),
            get_result=canned,
        )
        for name in ("chat", "reranker"):
            self.assertEqual(
                body[name]["error"], "transport failure: ConnectError"
            )
            self._assert_no_config_echo(body[name]["error"])

    def test_viewer_embeddings_http_failure_body_shape(self):
        settings.ollama_embedding_url = _SEED_EMBED_URL
        settings.ollama_chat_url = _SEED_CHAT_URL
        settings.reranker_url = _SEED_RERANK_URL
        body = self._probe(
            dns_map=self._public_dns(
                _SEED_EMBED_URL, _SEED_CHAT_URL, _SEED_RERANK_URL
            ),
            post_result=500,
        )
        embed = body["embeddings"]
        self.assertEqual(embed["url"], "embeddings")
        self.assertIs(embed["ok"], False)
        self.assertEqual(embed["status"], 500)
        self.assertEqual(embed["error"], "embedding inference failed (HTTP 500)")

    def test_empty_config_chat_url_error_is_clean_type_summary(self):
        # Shipped default: ollama_chat_url="". The guard raises URLBlocked
        # before any request; the non-admin error must be the clean
        # prefix + type name with the prefix intact and no shredded text.
        settings.ollama_embedding_url = _SEED_EMBED_URL
        settings.ollama_chat_url = ""
        settings.reranker_url = _SEED_RERANK_URL
        body = self._probe(
            dns_map=self._public_dns(_SEED_EMBED_URL, _SEED_RERANK_URL)
        )
        chat = body["chat"]
        self.assertEqual(chat["url"], "chat")
        self.assertEqual(chat["error"], "SSRF blocked: URLBlocked")
        self.assertEqual(chat["status"], None)
        self.assertIs(chat["ok"], False)

    def test_viewer_settings_view_redacts_api_key_set_flags(self):
        # chat_api_key_set / instant_api_key_set are computed response flags
        # in INFRA_REDACTED_FIELDS but not Settings attributes, so the frozen
        # structural sweep's hasattr filter cannot seed them (review
        # PRR-F3) — pin their role redaction here: a viewer must see both
        # coerced to False even when keys are configured, while an admin
        # sees the true values.
        settings.chat_api_key = "i660-secret-chat-key"
        settings.instant_api_key = "i660-secret-instant-key"
        viewer = self.client.get(
            "/api/settings",
            headers={"Authorization": "Bearer %s" % self.viewer_token},
        )
        self.assertEqual(viewer.status_code, 200, viewer.text)
        self.assertIs(viewer.json()["chat_api_key_set"], False)
        self.assertIs(viewer.json()["instant_api_key_set"], False)
        admin = self.client.get(
            "/api/settings",
            headers={"Authorization": "Bearer %s" % self.admin_token},
        )
        self.assertEqual(admin.status_code, 200, admin.text)
        self.assertIs(admin.json()["chat_api_key_set"], True)
        self.assertIs(admin.json()["instant_api_key_set"], True)

    def test_viewer_chat_reranker_http_failure_has_no_error_field(self):
        # PRR-F6 drift-hazard pin: the chat/reranker GET >=300 branches set
        # no error today. The redaction rewrite only covers entries recorded
        # in error_types (the two except sites), so if a future change adds
        # an error string on these branches it would ship unredacted to
        # non-admins — this test fails first (see the handler comment).
        settings.ollama_embedding_url = _SEED_EMBED_URL
        settings.ollama_chat_url = _SEED_CHAT_URL
        settings.reranker_url = _SEED_RERANK_URL
        body = self._probe(
            dns_map=self._public_dns(
                _SEED_EMBED_URL, _SEED_CHAT_URL, _SEED_RERANK_URL
            ),
            get_result=500,
        )
        for name in ("chat", "reranker"):
            self.assertNotIn("error", body[name])
            self.assertEqual(body[name]["status"], 500)
            self.assertIs(body[name]["ok"], False)
            self.assertEqual(body[name]["url"], name)
        self.assertEqual(body["embeddings"]["url"], "embeddings")
        self.assertIs(body["embeddings"]["ok"], True)
