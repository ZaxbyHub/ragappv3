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


class TestKmsLimitEnvelope:
    """DI-04 (r2 critic): the kms route's ge=1/le=500 envelope is new
    behavior and needs the same 422 pins the wiki route carries."""

    def _seed_one(self, route_env):
        route_env.seed_queue_job("kms", VAULT_A, status="running")

    def test_kms_limit_zero_rejected_422(self, route_env):
        self._seed_one(route_env)
        resp = route_env.client.get(
            "/api/kms/jobs",
            params={"vault_id": VAULT_A, "limit": 0},
            headers=route_env.member_headers(),
        )
        assert resp.status_code == 422

    def test_kms_limit_above_500_rejected_422(self, route_env):
        self._seed_one(route_env)
        resp = route_env.client.get(
            "/api/kms/jobs",
            params={"vault_id": VAULT_A, "limit": 501},
            headers=route_env.member_headers(),
        )
        assert resp.status_code == 422

    def test_kms_default_page_is_50(self, route_env):
        # Lease-on is the default: seed the UNIFIED queue (the lease-off
        # legacy-table branch is covered by TestLeaseOffBoundedLists).
        for _ in range(60):
            route_env.seed_queue_job("kms", VAULT_A, status="completed")
        resp = route_env.client.get(
            "/api/kms/jobs",
            params={"vault_id": VAULT_A},
            headers=route_env.member_headers(),
        )
        assert resp.status_code == 200
        assert len(resp.json()["jobs"]) == 50


class TestTerminalRemainder:
    """F-002 (r1 critic): the terminal arm fills the REMAINDER of the family
    window, so a family returns at most `limit` rows total."""

    def test_live_plus_terminal_capped_at_exact_limit(self, route_env):
        live_id = route_env.seed_queue_job("wiki", VAULT_A, status="running")
        for _ in range(55):
            route_env.seed_queue_job("wiki", VAULT_A, status="completed")
        rows = _family(route_env.client, route_env.member_headers(), "wiki")
        assert len(rows) == 50
        assert live_id in [j["id"] for j in rows]
        assert sum(1 for j in rows if j["status"] == "completed") == 49

    def test_full_live_backlog_leaves_no_terminal_room(self, route_env):
        for _ in range(60):
            route_env.seed_queue_job("wiki", VAULT_A, status="running")
        for _ in range(5):
            route_env.seed_queue_job("wiki", VAULT_A, status="completed")
        rows = _family(route_env.client, route_env.member_headers(), "wiki")
        assert len(rows) == 50
        assert all(j["status"] == "running" for j in rows)


class TestIngestProcessingPriority:
    """F-007 (r2 critic): processing rows rank ahead of pending inside the
    combined ingest window — a bulk upload's oldest (progress-bearing) rows
    cannot be evicted by newer pending rows."""

    def test_processing_row_survives_full_pending_window(self, route_env):
        def _seed(conn):
            conn.execute(
                "INSERT INTO files (vault_id, file_path, file_name, file_size, "
                "status, phase) VALUES (?, 'p0.txt', 'p0.txt', 10, "
                "'processing', 'embedding')",
                (VAULT_A,),
            )
            for i in range(55):
                conn.execute(
                    "INSERT INTO files (vault_id, file_path, file_name, file_size, "
                    "status) VALUES (?, ?, ?, 10, 'pending')",
                    (VAULT_A, f"p{i + 1}.txt", f"p{i + 1}.txt"),
                )
            conn.commit()

        route_env._with_conn(_seed)
        ingest = _family(route_env.client, route_env.member_headers(), "ingest")
        assert len(ingest) == 50
        statuses = [j["status"] for j in ingest]
        assert statuses[0] == "processing", "processing row must rank first"
        assert "processing" in statuses


class TestThreadOffloadWiring:
    """Stage B test-engineer F-003: the to_thread offload was unobserved —
    a sync revert passed the whole activity surface. Pin the wiring: the
    route must dispatch _collect_snapshot through asyncio.to_thread."""

    def test_route_offloads_collection_to_worker_thread(self, route_env, monkeypatch):
        import asyncio as asyncio_module
        import threading

        import app.api.routes.activity as activity_module

        # TestClient serves the ASGI app on an anyio PORTAL thread, so the
        # pytest caller's thread id is NOT the loop thread — comparing the
        # collection against the caller would let an inline (sync) revert
        # pass. The faithful capture: the `asyncio.to_thread(...)` CALL
        # expression evaluates synchronously inside the route coroutine,
        # i.e. ON the event-loop thread, so a to_thread spy records the loop
        # thread; a module-level _collect_snapshot wrapper records where the
        # collection actually ran (executor worker under correct code, the
        # loop thread under a sync revert). Auth dependencies also dispatch
        # through to_thread, so the loop-thread capture survives even a
        # route-only revert. A sync revert fails BOTH pins: no route
        # dispatch (the offload call is gone), or collection on the loop
        # thread (inline execution).
        loop_threads: list[int] = []
        collected_threads: list[int] = []
        real_to_thread = asyncio_module.to_thread
        real_collect = activity_module._collect_snapshot

        def to_thread_spy(fn, *args, **kwargs):
            loop_threads.append(threading.get_ident())
            return real_to_thread(fn, *args, **kwargs)

        def recording_collect(*args, **kwargs):
            collected_threads.append(threading.get_ident())
            return real_collect(*args, **kwargs)

        monkeypatch.setattr(activity_module.asyncio, "to_thread", to_thread_spy)
        monkeypatch.setattr(
            activity_module, "_collect_snapshot", recording_collect
        )
        route_env.seed_queue_job("wiki", VAULT_A, status="running")
        resp = route_env.client.get(
            "/api/activity/jobs", headers=route_env.member_headers()
        )
        assert resp.status_code == 200
        assert loop_threads, (
            "the activity route never dispatched through asyncio.to_thread "
            "(sync revert: the offload call is gone)"
        )
        assert collected_threads, "the route never invoked _collect_snapshot"
        loop_thread = loop_threads[0]
        assert all(t != loop_thread for t in collected_threads), (
            "the SQLite snapshot collection ran on the event-loop thread "
            "(sync revert of the to_thread offload)"
        )


class TestTerminalSettleRace:
    """Stage B reviewer round (F-002): the live and terminal reads are
    separate snapshots — a live row that SETTLES between them appears in
    both, and filtering it after a LIMIT-remaining read would leave the
    terminal remainder underfilled. The proxy connection flips the live row
    to completed right before the WIKI terminal query executes, reproducing
    the inter-read settle; the fix (over-fetch by |live|, filter, slice)
    must still fill exactly `remaining` distinct terminal rows."""

    def test_settling_live_row_does_not_underfill_remainder(self, route_env):
        import json

        import app.api.routes.activity as activity_module

        for _ in range(9):
            route_env.seed_queue_job("wiki", VAULT_A, status="completed")
        live_id = route_env.seed_queue_job("wiki", VAULT_A, status="running")

        class _SettleProxy:
            """Flips the live row to completed just before the WIKI terminal
            query executes (the inter-read settle)."""

            def __init__(self, conn, live_id):
                self._conn = conn
                self._live_id = live_id
                self.flips = 0

            def execute(self, sql, params=()):
                if (
                    self.flips == 0
                    and "queue = 'wiki'" in sql
                    and "'completed'" in sql
                ):
                    self._conn.execute(
                        "UPDATE jobs SET status = 'completed' WHERE id = ?",
                        (self._live_id,),
                    )
                    self._conn.commit()
                    self.flips += 1
                return self._conn.execute(sql, params)

            def commit(self):
                return self._conn.commit()

            def __getattr__(self, name):
                return getattr(self._conn, name)

        conn = route_env.pool.get_connection()
        try:
            proxy = _SettleProxy(conn, live_id)
            rows, _windows = activity_module._collect_snapshot(
                db=proxy,
                vault_json=json.dumps([VAULT_A]),
                is_admin=False,
                caller_id=MEMBER_ID,
                limit=5,
            )
        finally:
            route_env.pool.release_connection(conn)

        wiki_rows = [r for r in rows if r.family == "wiki"]
        # The inter-read settle MUST have fired exactly once — without it the
        # static seed already satisfies the id assertions (Stage B reviewer
        # round 2: non-vacuity pin for the race fixture itself).
        assert proxy.flips == 1, (
            f"the inter-read settle fixture never fired (flips={proxy.flips})"
        )
        # Live row (read as running, id 10) + exactly `remaining`=4 distinct
        # terminal rows — the vacated slot refilled from older history
        # (ids 9,8,7,6; the mutant's LIMIT-4 read misses id 6).
        assert len(wiki_rows) == 5, (
            f"family window must hold exactly limit=5 rows, got {len(wiki_rows)}"
        )
        wiki_ids = {r.id for r in wiki_rows}
        assert wiki_ids == {10, 9, 8, 7, 6}, (
            f"settled live row must not underfill the remainder: {sorted(wiki_ids)}"
        )


class TestDisabledGates:
    """K-17b (20b closeout): the wiki/KMS feature gates on the aggregate
    route (activity.py: if settings.wiki_enabled / kms_enabled) were
    unpinned — a mutant replacing them with `if True:` survived every
    prior activity test. With BOTH flags False, seeded wiki/kms rows must
    contribute ZERO rows (disabled subsystems are zero rows, never an
    error)."""

    def test_disabled_wiki_and_kms_contribute_zero_rows(self, route_env, monkeypatch):
        route_env.seed_queue_job("wiki", VAULT_A, status="running")
        route_env.seed_queue_job("kms", VAULT_A, status="completed")
        monkeypatch.setattr(settings, "wiki_enabled", False)
        monkeypatch.setattr(settings, "kms_enabled", False)
        resp = route_env.client.get(
            "/api/activity/jobs", headers=route_env.member_headers()
        )
        assert resp.status_code == 200, resp.text
        families = {j["family"] for j in resp.json()["jobs"]}
        assert "wiki" not in families, (
            "disabled wiki gate must contribute zero wiki rows"
        )
        assert "kms" not in families, (
            "disabled kms gate must contribute zero kms rows"
        )
