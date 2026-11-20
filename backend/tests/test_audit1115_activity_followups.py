"""Companion pins for the #1157/#1158 activity aggregate (4.5 review round).

Non-frozen companions to tests/test_audit1115_e1_activity.py — same route_env
fixture (imported), pinning the guarantees the review round established:

- F1 (Critical): wiki/KMS error text passes the family read-time mask
  (redact_legacy_error_text) in BOTH lease branches — pre-#951 rows carry
  raw ``str(exc)`` in the error column and must never reach a vault reader
  through the aggregate.
- F2 (Important): live jobs cannot be evicted by terminal history — an
  older still-running job stays visible behind a full page of newer
  terminal rows (wiki unified + legacy, kms, draft-room, reindex incl. a
  legacy ``interrupted`` terminal row and a NULL-vault global).
- P-8 promised fixtures: bounded default + explicit limit in the LEASE-OFF
  branch for both wiki and kms list routes; a fractional-progress ingest
  row through the aggregate.
"""

CSRF_TEST_POLICY = "naive"  # GET-only suite; no csrf dependency is exercised

import pytest

from app.config import settings
from tests.test_audit1115_e1_activity import (  # noqa: F401 (fixture import)
    MEMBER_ID,
    VAULT_A,
)
from tests.test_audit1115_e1_activity import (
    route_env as route_env,
)

RAW_ERROR = "connection failed: sqlite:///db with password hunter2"


def _family(client, headers, family):
    resp = client.get("/api/activity/jobs", headers=headers)
    assert resp.status_code == 200, resp.text
    return [j for j in resp.json()["jobs"] if j["family"] == family]


class TestDraftOwnerBoundary:
    """Final-critic round-1 finding: the draft owner boundary (SPEC 9.1 —
    "draft existence is never disclosed across owners") must hold through the
    aggregate for EVERY role. A second user's draft+job in a vault the member
    (and the superadmin) can read must never surface — the neutralized-owner
    mutation (``d.created_by = ? OR 1=1``) surviving every prior test is the
    exact regression this pins."""

    OTHER_USER_ID = 42

    def _seed_foreign_draft(self, route_env):
        def _seed(conn):
            conn.execute(
                "INSERT INTO users (id, username, hashed_password, full_name, role, "
                "is_active) VALUES (?, 'other', 'pw-hash', 'O', 'member', 1)",
                (self.OTHER_USER_ID,),
            )
            cur = conn.execute(
                "INSERT INTO drafts (vault_id, created_by, title, mode) "
                "VALUES (?, ?, 'secret foreign draft', 'compose')",
                (VAULT_A, self.OTHER_USER_ID),
            )
            draft_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
                "status, max_model_calls, timeout_seconds) "
                "VALUES (?, ?, ?, 'compile', 'running', 5, 60)",
                (draft_id, VAULT_A, self.OTHER_USER_ID),
            )
            conn.commit()
            return draft_id

        return route_env._with_conn(_seed)

    def test_member_never_sees_another_users_draft_rows(self, route_env):
        foreign_draft_id = self._seed_foreign_draft(route_env)
        rows = _family(route_env.client, route_env.member_headers(), "draft-room")
        assert all(j["draft_id"] != foreign_draft_id for j in rows)
        assert all("secret foreign draft" != j["title"] for j in rows)

    def test_superadmin_also_never_sees_another_users_draft_rows(self, route_env):
        # Admins read ALL VAULTS for the other families, but drafts stay
        # owner-scoped for every role — the draft-room routes' own boundary.
        foreign_draft_id = self._seed_foreign_draft(route_env)
        rows = _family(route_env.client, route_env.superadmin_headers(), "draft-room")
        assert all(j["draft_id"] != foreign_draft_id for j in rows)
        assert all("secret foreign draft" != j["title"] for j in rows)


class TestErrorRedaction:
    """F1: the aggregate masks wiki/KMS error text like the family readers."""

    def test_unified_branch_masks_raw_legacy_error_text(self, route_env):
        def _seed(conn):
            import json as _json

            from app.services.job_lease import ensure_jobs_schema

            ensure_jobs_schema(conn)
            cur = conn.execute(
                "INSERT INTO jobs (queue, payload_json, status, error) VALUES "
                "('wiki', ?, 'failed', ?)",
                (_json.dumps({"vault_id": VAULT_A, "trigger_type": "manual"}), RAW_ERROR),
            )
            conn.commit()
            return cur.lastrowid

        route_env._with_conn(_seed)
        rows = _family(route_env.client, route_env.member_headers(), "wiki")
        assert rows
        for row in rows:
            assert "hunter2" not in (row.get("error") or "")
            assert "sqlite:///" not in (row.get("error") or "")

    def test_legacy_branch_masks_raw_legacy_error_text(
        self, route_env, monkeypatch
    ):
        monkeypatch.setattr(settings, "wiki_kms_job_lease_enabled", False)

        def _seed(conn):
            cur = conn.execute(
                "INSERT INTO wiki_compile_jobs (vault_id, trigger_type, status, error) "
                "VALUES (?, 'manual', 'failed', ?)",
                (VAULT_A, RAW_ERROR),
            )
            conn.commit()
            return cur.lastrowid

        route_env._with_conn(_seed)
        rows = _family(route_env.client, route_env.member_headers(), "wiki")
        assert rows
        for row in rows:
            assert "hunter2" not in (row.get("error") or "")

    def test_kms_branch_masks_raw_legacy_error_text(self, route_env):
        def _seed(conn):
            import json as _json

            from app.services.job_lease import ensure_jobs_schema

            ensure_jobs_schema(conn)
            cur = conn.execute(
                "INSERT INTO jobs (queue, payload_json, status, error) VALUES "
                "('kms', ?, 'failed', ?)",
                (_json.dumps({"vault_id": VAULT_A, "trigger_type": "manual"}), RAW_ERROR),
            )
            conn.commit()
            return cur.lastrowid

        route_env._with_conn(_seed)
        rows = _family(route_env.client, route_env.member_headers(), "kms")
        assert rows
        for row in rows:
            assert "hunter2" not in (row.get("error") or "")


class TestLiveEviction:
    """F2: terminal history cannot hide a live job."""

    def test_older_live_wiki_job_survives_a_full_terminal_page(self, route_env):
        # The LIVE row lands FIRST (older id), then a full default page of
        # newer terminal rows lands on top of it.
        live_id = route_env.seed_queue_job("wiki", VAULT_A, status="running")
        for _ in range(55):
            route_env.seed_queue_job("wiki", VAULT_A, status="completed")
        wiki_ids = [j["id"] for j in _family(
            route_env.client, route_env.member_headers(), "wiki"
        )]
        assert live_id in wiki_ids, "live job evicted by terminal history"

    def test_older_live_legacy_wiki_job_survives_terminal_page(
        self, route_env, monkeypatch
    ):
        monkeypatch.setattr(settings, "wiki_kms_job_lease_enabled", False)

        def _seed(conn):
            live = conn.execute(
                "INSERT INTO wiki_compile_jobs (vault_id, trigger_type, status) "
                "VALUES (?, 'manual', 'running')",
                (VAULT_A,),
            ).lastrowid
            for _ in range(55):
                conn.execute(
                    "INSERT INTO wiki_compile_jobs (vault_id, trigger_type, status) "
                    "VALUES (?, 'manual', 'completed')",
                    (VAULT_A,),
                )
            conn.commit()
            return live

        live_id = route_env._with_conn(_seed)
        wiki_ids = [j["id"] for j in _family(
            route_env.client, route_env.member_headers(), "wiki"
        )]
        assert live_id in wiki_ids

    def test_older_live_kms_job_survives_terminal_page(self, route_env):
        live_id = route_env.seed_queue_job("kms", VAULT_A, status="running")
        for _ in range(55):
            route_env.seed_queue_job("kms", VAULT_A, status="failed")
        kms_ids = [j["id"] for j in _family(
            route_env.client, route_env.member_headers(), "kms"
        )]
        assert live_id in kms_ids

    def test_older_live_draft_job_survives_terminal_page(self, route_env):
        def _seed(conn):
            conn.execute(
                "INSERT INTO drafts (vault_id, created_by, title, mode) "
                "VALUES (?, ?, 'd', 'compose')",
                (VAULT_A, MEMBER_ID),
            )
            draft_id = conn.execute(
                "SELECT MAX(id) FROM drafts"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
                "status, max_model_calls, timeout_seconds) "
                "VALUES (?, ?, ?, 'compile', 'running', 5, 60)",
                (draft_id, VAULT_A, MEMBER_ID),
            )
            for _ in range(55):
                conn.execute(
                    "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
                    "status, max_model_calls, timeout_seconds) "
                    "VALUES (?, ?, ?, 'compile', 'completed', 5, 60)",
                    (draft_id, VAULT_A, MEMBER_ID),
                )
            conn.commit()

        route_env._with_conn(_seed)
        drafts = _family(route_env.client, route_env.member_headers(), "draft-room")
        assert any(j["status"] == "running" for j in drafts)

    def test_admin_reindex_live_survives_terminal_page(self, route_env):
        live_id = route_env.seed_queue_job("reindex", VAULT_A, status="running")
        for _ in range(55):
            route_env.seed_queue_job("reindex", VAULT_A, status="completed")
        reindex_ids = [j["id"] for j in _family(
            route_env.client, route_env.superadmin_headers(), "reindex"
        )]
        assert live_id in reindex_ids

    def test_admin_legacy_reindex_null_vault_and_interrupted(
        self, route_env, monkeypatch
    ):
        monkeypatch.setattr(settings, "reindex_job_lease_enabled", False)

        def _seed(conn):
            conn.execute(
                "INSERT INTO document_reindex_jobs (vault_id, trigger_type, status) "
                "VALUES (NULL, 'api', 'interrupted')"
            )
            conn.execute(
                "INSERT INTO document_reindex_jobs (vault_id, trigger_type, status) "
                "VALUES (?, 'api', 'running')",
                (VAULT_A,),
            )
            conn.commit()

        route_env._with_conn(_seed)
        reindex = _family(route_env.client, route_env.superadmin_headers(), "reindex")
        statuses = {j["status"] for j in reindex}
        assert "running" in statuses
        assert "failed" in statuses  # interrupted -> failed
        assert "interrupted" not in statuses
        assert any(j["vault_id"] is None for j in reindex)


class TestLeaseOffBoundedLists:
    """P-8 promised: the #1157 default bound holds in the LEASE-OFF branch."""

    def _seed_many_legacy(self, route_env, table):
        def _seed(conn):
            for _ in range(60):
                conn.execute(
                    f"INSERT INTO {table} (vault_id, trigger_type, status) "  # noqa: S608
                    f"VALUES (?, 'manual', 'completed')",
                    (VAULT_A,),
                )
            conn.commit()

        route_env._with_conn(_seed)

    def test_wiki_jobs_bounded_in_lease_off_branch(self, route_env, monkeypatch):
        monkeypatch.setattr(settings, "wiki_kms_job_lease_enabled", False)
        self._seed_many_legacy(route_env, "wiki_compile_jobs")
        default = route_env.client.get(
            "/api/wiki/jobs",
            params={"vault_id": VAULT_A},
            headers=route_env.member_headers(),
        )
        explicit = route_env.client.get(
            "/api/wiki/jobs",
            params={"vault_id": VAULT_A, "limit": 60},
            headers=route_env.member_headers(),
        )
        assert default.status_code == 200
        assert len(default.json()["jobs"]) == 50
        assert explicit.status_code == 200
        assert len(explicit.json()["jobs"]) == 60

    def test_kms_jobs_bounded_in_lease_off_branch(self, route_env, monkeypatch):
        monkeypatch.setattr(settings, "wiki_kms_job_lease_enabled", False)
        self._seed_many_legacy(route_env, "kms_compile_jobs")
        default = route_env.client.get(
            "/api/kms/jobs",
            params={"vault_id": VAULT_A},
            headers=route_env.member_headers(),
        )
        explicit = route_env.client.get(
            "/api/kms/jobs",
            params={"vault_id": VAULT_A, "limit": 60},
            headers=route_env.member_headers(),
        )
        assert default.status_code == 200
        assert len(default.json()["jobs"]) == 50
        assert explicit.status_code == 200
        assert len(explicit.json()["jobs"]) == 60


class TestFractionalProgress:
    """P-8 promised: fractional ingest progress survives the response."""

    def test_fractional_progress_percent_round_trips(self, route_env):
        def _seed(conn):
            conn.execute(
                "INSERT INTO files (vault_id, file_path, file_name, file_size, status, "
                "phase, progress_percent) VALUES (?, 'f.txt', 'f.txt', 10, "
                "'processing', 'embedding', 12.5)",
                (VAULT_A,),
            )
            conn.commit()

        route_env._with_conn(_seed)
        ingest = _family(route_env.client, route_env.member_headers(), "ingest")
        assert ingest
        assert ingest[0]["progress_percent"] == 12.5
        assert ingest[0]["phase"] == "embedding"


@pytest.mark.parametrize("unused", [pytest.param(None, id="singleton")])
def test_companion_file_collected(unused):
    """Marker so a vacuous collection is observable."""
