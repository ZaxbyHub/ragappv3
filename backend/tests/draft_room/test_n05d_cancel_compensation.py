"""Arm's-length acceptance checks C9/C11/C20 for the draft-capability-liveness
trace (issues #902/#905/#911/#913), upload compensation cancellation side.

Frozen specification.

* C9 (DISCRIMINATING, ``TestN05DRollbackUnderCancellation``): drive the REAL
  upload route so the enqueue phase fails (the established
  ``TestN05UploadCompensation`` shape: ``enqueue_parse_job`` raises a domain
  conflict) and deliver a cancellation to the request while the
  compensation's FIRST step — the staged-file unlink — is still pending.
  The reserved ``draft_inputs`` row must STILL be rolled back (row absent
  afterwards). Three delivery shapes are pinned: one native
  ``task.cancel()``, a REPEATED native cancellation (cancel delivered
  twice), and an anyio/Starlette-style outer cancel-scope cancellation
  (the request runs inside an ``anyio`` task group whose scope is
  cancelled — the shape Starlette uses for client disconnects). RED at
  base (the cancellation escapes the unshielded compensation and skips the
  row rollback), GREEN after the fix.
* C11 (PRESERVING, GREEN at base, ``TestN05DCancellationPropagation``): the
  ``CancelledError`` must still propagate out of the request for BOTH
  delivery shapes — native ``task.cancel()`` and an outer anyio cancel
  scope — the rollback fix must neither swallow it nor convert it into a
  normal response.
* C20 (DISCRIMINATING, ``TestN05DCancelMidStepIntegrity``): deliver the
  cancellation while the FINALIZE step is pending, and separately while the
  parse-job ENQUEUE step is pending. The ``CancelledError`` must still
  propagate out of the request (asserted here too), and after the request
  unwinds and every helper settles, the upload must never be left
  half-committed: either (draft_inputs row present AND final bytes present
  AND a parse job row present) or (no row AND no final bytes). A reserved
  ``draft_inputs`` row with no parse job must not exist. RED at base (a
  CancelledError at either await skips every cleanup handler), GREEN after
  the fix. Teardown-ordering legs additionally release the blocked op only
  AFTER the request task exited and its ``get_db`` dependency connection
  was returned to the pool (a recording dependency override gates the
  release) — the same invariant must hold when the commit work proceeds
  strictly after the request's connection teardown. The finalize wrapper
  performs the REAL move after release; the enqueue wrapper RAISES after
  release (the op fails once finally attempted), so a correct fix settles
  into commit (finalize legs) or compensation (enqueue legs) rather than a
  half state. Anyio cancel-scope scenarios (the Starlette client-disconnect
  shape) run for BOTH steps: the scope is cancelled while finalize (and
  separately enqueue) is pending; the cancellation must propagate and the
  settled upload must satisfy the same half-commit invariant.

Mechanics: the request runs through ``httpx.ASGITransport`` against the real
app object (same dependency overrides and auth headers the route base
installs) on a private event loop, so the request coroutine can be cancelled
from the test at a controlled point. The blocked step parks on a threading
event; ordering is: deliver the cancellation, RELEASE the blocked step
immediately (a correct implementation may legitimately drain the in-flight
compensation before propagating, so the step must not stay blocked while
the request task finishes), await the request task, wait — event-driven —
for the blocked step's thread to finish its real work, then drain every
remaining task on the loop before any DB/filesystem assertion. All waits
are threading/asyncio events (no polling sleeps).
"""

import asyncio
import contextlib
import os
import pathlib
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

from unittest.mock import patch

import httpx

from app.main import app
from app.services.draft_store import DraftConflictError, DraftStore
from tests.draft_room.test_draft_compile_routes import CompileRouteTestBase

_UPLOAD_BYTES = b"Bytes whose upload is cancelled mid-flight while a step is pending."


@contextlib.contextmanager
def _noop():
    yield None


async def _settle_request_task(task):
    """Await the request task and classify what left the request boundary."""
    try:
        response = await task
        return {"propagated": False, "status": response.status_code, "error": None}
    except asyncio.CancelledError:
        return {"propagated": True, "status": None, "error": None}
    except BaseException as exc:  # reported, never swallowed
        return {"propagated": False, "status": None, "error": repr(exc)}


async def _settle_helpers(finished):
    """Event-driven wait until the blocked step's thread has finished its
    real work after being released."""
    loop = asyncio.get_running_loop()
    done = await loop.run_in_executor(None, finished.wait, 30)
    if not done:
        raise AssertionError("the released blocked step never finished its work")


async def _drain_pending_tasks():
    """Event-driven drain: wait until every other task on this loop is done,
    so any shielded continuation (compensation, audit tails) has settled
    before the caller asserts DB/filesystem state."""
    for _ in range(100):
        pending = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ]
        if not pending:
            return
        await asyncio.wait(pending, timeout=30)
    raise AssertionError("loop tasks never drained after the cancellation")


def _build_step_patch(step, entered, release, finished):
    """The patch that parks one route step on threading events.

    ``compensation_unlink`` blocks the compensation's unlink of the
    FINALIZED bytes (and then performs the real unlink after release);
    ``finalize`` blocks the staged->final move and then performs the REAL
    move after release (a committed outcome carries real bytes);
    ``enqueue`` blocks the parse-job enqueue and then RAISES after release
    (the op fails once finally attempted — at base nothing cleans up, so a
    reserved row with no parse job survives; after the fix the detached
    commit compensates: row and final bytes both gone).
    """
    from app.services.draft_input_storage import DraftInputStorage

    if step == "compensation_unlink":
        real_unlink = pathlib.Path.unlink

        def _blocking_unlink(path_self, *args, **kwargs):
            text = str(path_self)
            if (
                "draft-room" in text
                and ".incoming" not in text
                and ".trash" not in text
            ):
                entered.set()
                if not release.wait(timeout=300):
                    raise AssertionError(
                        "the blocked compensation unlink was never released"
                    )
                result = real_unlink(path_self, *args, **kwargs)
                finished.set()
                return result
            return real_unlink(path_self, *args, **kwargs)

        return patch.object(pathlib.Path, "unlink", _blocking_unlink)
    if step == "finalize":
        real_finalize = DraftInputStorage.finalize

        def _blocking_finalize(storage_self, staged, relpath):
            entered.set()
            if not release.wait(timeout=300):
                raise AssertionError("the blocked finalize step was never released")
            result = real_finalize(storage_self, staged, relpath)
            finished.set()
            return result

        return patch.object(DraftInputStorage, "finalize", _blocking_finalize)
    if step == "enqueue":

        def _blocking_failing_enqueue(store_self, **kwargs):
            entered.set()
            if not release.wait(timeout=300):
                raise AssertionError("the blocked enqueue step was never released")
            finished.set()
            raise DraftConflictError("the enqueue failed once finally attempted")

        return patch.object(DraftStore, "enqueue_parse_job", _blocking_failing_enqueue)
    raise ValueError(f"unknown blocking step {step!r}")  # pragma: no cover


def _enqueue_failure_patch():
    """The C9/C11 scenario's enqueue-phase failure (domain conflict)."""
    return patch.object(
        DraftStore,
        "enqueue_parse_job",
        side_effect=DraftConflictError("a parse job is already active"),
    )


async def _run_request_native(
    client, url, request_headers, entered, release, *, twice, defer_release
):
    task = asyncio.create_task(
        client.post(
            url,
            data={"role": "manuscript", "authority": "primary"},
            files={"file": ("notes.txt", _UPLOAD_BYTES, "text/plain")},
            headers=request_headers,
        )
    )
    try:
        loop = asyncio.get_running_loop()
        if not await loop.run_in_executor(None, entered.wait, 30):
            release.set()
            try:
                response = await asyncio.wait_for(task, timeout=10)
                detail = (
                    f"request finished early with status="
                    f"{response.status_code} body={response.text[:400]}"
                )
            except BaseException as exc:
                detail = f"request task raised {exc!r}"
            raise AssertionError(
                "the blocked step was never entered; the scenario did not "
                f"reach the pending step ({detail})"
            )
        task.cancel()
        if twice:
            # A second cancellation delivered before the first is consumed.
            task.cancel()
        if not defer_release:
            # Release the blocked worker thread IMMEDIATELY after the
            # cancellation(s) were delivered — do not wait for the request
            # task. (With defer_release the DRIVER owns the release: it
            # waits for the request task to exit and the dependency
            # teardown to run first — the teardown-ordering shape.)
            release.set()
        return await _settle_request_task(task)
    finally:
        if not defer_release:
            release.set()
        await asyncio.gather(task, return_exceptions=True)


async def _run_request_in_cancel_scope(client, url, request_headers, entered, release):
    """Starlette-shaped delivery: the request runs as a task inside an
    anyio task group and the group's cancel scope is cancelled while the
    blocked step is pending."""
    import anyio

    holder = {"resp": None, "exc": None}

    async def _request():
        try:
            holder["resp"] = await client.post(
                url,
                data={"role": "manuscript", "authority": "primary"},
                files={"file": ("notes.txt", _UPLOAD_BYTES, "text/plain")},
                headers=request_headers,
            )
        except BaseException as exc:  # reported, never swallowed
            holder["exc"] = exc

    async with anyio.create_task_group() as tg:
        tg.start_soon(_request)
        loop = asyncio.get_running_loop()
        if not await loop.run_in_executor(None, entered.wait, 30):
            release.set()
            tg.cancel_scope.cancel()
            raise AssertionError(
                "the blocked step was never entered; the scenario did not "
                "reach the pending step (anyio scope mode)"
            )
        tg.cancel_scope.cancel()
        # Release the blocked worker thread IMMEDIATELY after the scope
        # cancellation was delivered.
        release.set()

    response, exc = holder["resp"], holder["exc"]
    if response is not None:
        return {"propagated": False, "status": response.status_code, "error": None}
    if exc is None:
        raise AssertionError("the scoped request produced neither response nor error")
    if isinstance(exc, (asyncio.CancelledError, anyio.get_cancelled_exc_class())):
        return {"propagated": True, "status": None, "error": None}
    return {"propagated": False, "status": None, "error": repr(exc)}


async def _drive_cancelled_upload(
    draft_id: int,
    headers: dict,
    *,
    step: str = "compensation_unlink",
    mode: str = "native",
    enqueue_failure: bool = True,
    defer_release: bool = False,
    teardown_gate: threading.Event | None = None,
) -> dict:
    """One cancelled-upload request; returns what left the request boundary.

    ``propagated`` is True exactly when the request ended in a cancellation
    exception; ``error`` carries any other exception that escaped instead.
    With ``defer_release`` the blocked step is released only AFTER the
    request task exited and (when ``teardown_gate`` is given) the gate
    fired — the teardown-ordering shape (the request's dependency
    connection is back in the pool before the blocked op proceeds).
    """
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    # The route base bakes a fingerprint computed from an EMPTY user-agent
    # into its tokens (TestClient's shape); httpx would send its own
    # user-agent and fail the fingerprint check with 401 token_invalid.
    request_headers = dict(headers)
    request_headers["user-agent"] = ""
    url = f"/api/draft-room/drafts/{draft_id}/inputs"

    step_patch = _build_step_patch(step, entered, release, finished)
    failure_patch = _enqueue_failure_patch() if enqueue_failure else _noop()
    try:
        with step_patch, failure_patch:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                if mode == "anyio_scope":
                    outcome = await _run_request_in_cancel_scope(
                        client, url, request_headers, entered, release
                    )
                else:
                    outcome = await _run_request_native(
                        client,
                        url,
                        request_headers,
                        entered,
                        release,
                        twice=(mode == "native_twice"),
                        defer_release=defer_release,
                    )
                if defer_release:
                    if teardown_gate is not None:
                        loop = asyncio.get_running_loop()
                        if not await loop.run_in_executor(None, teardown_gate.wait, 30):
                            raise AssertionError(
                                "the request's dependency connection was never "
                                "returned (teardown did not run before release)"
                            )
                    release.set()
                await _settle_helpers(finished)
                await _drain_pending_tasks()
    finally:
        release.set()
    return outcome


def run_cancelled_upload(draft_id: int, headers: dict, mode: str = "native") -> dict:
    return asyncio.run(_drive_cancelled_upload(draft_id, headers, mode=mode))


def run_cancelled_upload_at_step(
    draft_id: int,
    headers: dict,
    step: str,
    *,
    defer_release: bool = False,
    teardown_gate: threading.Event | None = None,
) -> dict:
    return asyncio.run(
        _drive_cancelled_upload(
            draft_id,
            headers,
            step=step,
            enqueue_failure=False,
            defer_release=defer_release,
            teardown_gate=teardown_gate,
        )
    )


def run_cancelled_upload_anyio_at_step(draft_id: int, headers: dict, step: str) -> dict:
    """Cancel-scope delivery (Starlette client-disconnect shape) with the
    given route step parked; the blocked step is released immediately after
    the scope cancellation is delivered."""
    return asyncio.run(
        _drive_cancelled_upload(
            draft_id, headers, step=step, enqueue_failure=False, mode="anyio_scope"
        )
    )


class _N05DCancelBase(CompileRouteTestBase):
    def _draft_input_row_count(self, draft_id):
        conn = self._connection_pool.get_connection()
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM draft_inputs WHERE draft_id = ?", (draft_id,)
            ).fetchone()[0]
        finally:
            self._connection_pool.release_connection(conn)

    def _scenario(self, mode="native"):
        draft_id = self._create_draft(title="N05D cancel compensation").json()["id"]
        outcome = run_cancelled_upload(draft_id, dict(self._owner_headers()), mode)
        self.assertIsNone(
            outcome["error"],
            f"the scenario itself must not error at the request boundary, got "
            f"{outcome['error']}",
        )
        return draft_id, outcome


class TestN05DRollbackUnderCancellation(_N05DCancelBase):
    """C9 (DISCRIMINATING)."""

    def test_reserved_input_row_is_rolled_back_despite_cancellation(self):
        """RED at base (the cancellation escapes the unshielded compensation
        and the reserved row survives), GREEN after the fix: cancelling the
        request while the compensation's unlink step is pending must not
        strand the reserved ``draft_inputs`` row (a stranded row 409s every
        re-upload on ``UNIQUE(draft_id, content_sha256)`` until restart)."""
        draft_id, _outcome = self._scenario(mode="native")
        self.assertEqual(
            self._draft_input_row_count(draft_id),
            0,
            "the reserved draft_inputs row must still be rolled back when the "
            "request is cancelled while the compensation's first step is "
            "pending",
        )

    def test_reserved_input_row_is_rolled_back_with_repeated_cancellation(self):
        """RED at base, GREEN after the fix: a SECOND cancellation delivered
        before the first is consumed must not abort the rollback either."""
        draft_id, _outcome = self._scenario(mode="native_twice")
        self.assertEqual(
            self._draft_input_row_count(draft_id),
            0,
            "the reserved draft_inputs row must still be rolled back when the "
            "request is cancelled while the compensation's first step is "
            "pending",
        )

    def test_reserved_input_row_is_rolled_back_under_an_anyio_cancel_scope(self):
        """RED at base, GREEN after the fix: the cancellation delivered the
        way Starlette delivers client disconnects (an outer anyio cancel
        scope) must still not strand the reserved row — and the cancellation
        must still PROPAGATE (rollback alone is not proof the scope-shaped
        delivery was honored)."""
        draft_id, outcome = self._scenario(mode="anyio_scope")
        self.assertIsNone(outcome["error"], f"scenario error: {outcome['error']}")
        self.assertTrue(
            outcome["propagated"],
            "the cancellation delivered through the outer anyio cancel scope "
            "must still propagate out of the upload request",
        )
        self.assertEqual(
            self._draft_input_row_count(draft_id),
            0,
            "the reserved draft_inputs row must still be rolled back when the "
            "request is cancelled while the compensation's first step is "
            "pending",
        )


class TestN05DCancellationPropagation(_N05DCancelBase):
    """C11 (PRESERVING, GREEN at base, must stay green after the fix)."""

    def test_cancelled_error_still_propagates_out_of_the_request(self):
        """GREEN at base: the ``CancelledError`` delivered mid-compensation
        propagates out of the request (FastAPI/Starlette treat it as client
        disconnect). Whatever shielding the rollback fix adds, it must not
        swallow it or convert it into a normal response."""
        _draft_id, outcome = self._scenario(mode="native")
        self.assertTrue(
            outcome["propagated"],
            "asyncio.CancelledError must still propagate out of the upload "
            "request; the compensation shielding must not swallow it",
        )

    def test_cancelled_error_still_propagates_under_an_anyio_cancel_scope(self):
        """GREEN at base, must stay green: the Starlette-shaped delivery (the
        request runs inside an anyio task group whose scope is cancelled)
        must also leave the cancellation propagating out of the request —
        neither swallowed nor converted into a normal response."""
        _draft_id, outcome = self._scenario(mode="anyio_scope")
        self.assertTrue(
            outcome["propagated"],
            "asyncio.CancelledError must still propagate out of the upload "
            "request when the cancellation is delivered through an outer "
            "anyio cancel scope; the compensation shielding must not swallow "
            "it",
        )


class TestN05DCancelMidStepIntegrity(_N05DCancelBase):
    """C20 (DISCRIMINATING)."""

    def _parse_job_count(self, draft_id):
        conn = self._connection_pool.get_connection()
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM draft_jobs WHERE draft_id = ? "
                "AND job_type = 'parse_input'",
                (draft_id,),
            ).fetchone()[0]
        finally:
            self._connection_pool.release_connection(conn)

    def _final_files(self, draft_id):
        from pathlib import Path

        from app.config import settings

        project_dir = (
            Path(settings.data_dir) / "draft-room" / str(self.OWNER_ID) / str(draft_id)
        )
        if not project_dir.is_dir():
            return []
        return sorted(str(p) for p in project_dir.rglob("*") if p.is_file())

    def _assert_no_half_commit(self, draft_id, step):
        conn = self._connection_pool.get_connection()
        try:
            row = conn.execute(
                "SELECT id FROM draft_inputs WHERE draft_id = ?", (draft_id,)
            ).fetchone()
        finally:
            self._connection_pool.release_connection(conn)
        if row is None:
            self.assertEqual(
                self._final_files(draft_id),
                [],
                f"with no reserved row there must be no final bytes either "
                f"(cancellation during the {step} step)",
            )
        else:
            self.assertGreaterEqual(
                self._parse_job_count(draft_id),
                1,
                "a reserved draft_inputs row with no parse job must not "
                f"exist (half-committed upload after cancellation during the "
                f"{step} step)",
            )
            self.assertTrue(
                self._final_files(draft_id),
                f"a reserved draft_inputs row must be backed by finalized "
                f"bytes (cancellation during the {step} step)",
            )

    def _run_step_scenario(self, step):
        draft_id = self._create_draft(title=f"N05D cancel {step}").json()["id"]
        outcome = run_cancelled_upload_at_step(
            draft_id, dict(self._owner_headers()), step
        )
        self.assertIsNone(
            outcome["error"],
            f"the scenario itself must not error at the request boundary, got "
            f"{outcome['error']}",
        )
        self.assertTrue(
            outcome["propagated"],
            "asyncio.CancelledError must still propagate out of the upload "
            f"request cancelled during the {step} step; mid-step cleanup must "
            "not swallow it",
        )
        return draft_id

    def test_cancel_during_finalize_leaves_no_half_committed_upload(self):
        draft_id = self._run_step_scenario("finalize")
        self._assert_no_half_commit(draft_id, "finalize")

    def test_cancel_during_enqueue_leaves_no_half_committed_upload(self):
        draft_id = self._run_step_scenario("enqueue")
        self._assert_no_half_commit(draft_id, "enqueue")

    # -- teardown-ordering legs: the blocked step is released only AFTER the
    # -- request task exited and its dependency connection was returned. ----

    def _install_teardown_recording_db(self):
        """Override ``get_db`` with a generator whose finally fires a
        threading event when FastAPI's AsyncExitStack unwinds the request's
        dependency (the connection is back in the pool). Returns the
        event and a restore callable."""
        from app.api.deps import get_db

        pool = self._connection_pool
        returned = threading.Event()
        previous = app.dependency_overrides.get(get_db)

        def recording_get_db():
            conn = pool.get_connection()
            try:
                yield conn
            finally:
                returned.set()
                pool.release_connection(conn)

        app.dependency_overrides[get_db] = recording_get_db

        def restore():
            if previous is not None:
                app.dependency_overrides[get_db] = previous
            else:
                app.dependency_overrides.pop(get_db, None)

        return returned, restore

    def _run_teardown_ordered_step_scenario(self, step):
        returned, restore_db = self._install_teardown_recording_db()
        try:
            draft_id = self._create_draft(title=f"N05D teardown {step}").json()["id"]
            outcome = run_cancelled_upload_at_step(
                draft_id,
                dict(self._owner_headers()),
                step,
                defer_release=True,
                teardown_gate=returned,
            )
        finally:
            restore_db()
        self.assertIsNone(
            outcome["error"],
            f"the scenario itself must not error at the request boundary, got "
            f"{outcome['error']}",
        )
        self.assertTrue(
            outcome["propagated"],
            "asyncio.CancelledError must still propagate out of the upload "
            f"request cancelled during the {step} step, even when the blocked "
            "op is released only after the request's dependency teardown",
        )
        return draft_id

    def test_finalize_released_after_request_teardown_leaves_no_half_commit(self):
        """RED at base (the reserved row survives with no parse job), GREEN
        after the fix: the blocked finalize is released only after the
        request task exited AND its dependency connection was returned; the
        upload must still settle into a definite committed or compensated
        outcome."""
        draft_id = self._run_teardown_ordered_step_scenario("finalize")
        self._assert_no_half_commit(draft_id, "finalize (teardown-ordered)")

    def test_enqueue_released_after_request_teardown_leaves_no_half_commit(self):
        """RED at base (the reserved row survives; the enqueue fails once
        finally attempted and nothing compensates), GREEN after the fix."""
        draft_id = self._run_teardown_ordered_step_scenario("enqueue")
        self._assert_no_half_commit(draft_id, "enqueue (teardown-ordered)")

    # -- anyio cancel-scope scenarios: the Starlette client-disconnect shape,
    # -- delivered while each step is pending. --------------------------------

    def _run_anyio_step_scenario(self, step):
        draft_id = self._create_draft(title=f"N05D anyio scope {step}").json()["id"]
        outcome = run_cancelled_upload_anyio_at_step(
            draft_id, dict(self._owner_headers()), step
        )
        self.assertIsNone(
            outcome["error"],
            f"the scenario itself must not error at the request boundary, got "
            f"{outcome['error']}",
        )
        self.assertTrue(
            outcome["propagated"],
            "the cancel-scope cancellation delivered while the "
            f"{step} step is pending must still propagate out of the upload "
            "request",
        )
        return draft_id

    def test_anyio_scope_cancel_during_finalize_leaves_no_half_commit(self):
        """RED at base, GREEN after the fix: the cancel scope is cancelled
        while the finalize step is pending — the cancellation propagates and
        the settled upload is never half-committed."""
        draft_id = self._run_anyio_step_scenario("finalize")
        self._assert_no_half_commit(draft_id, "finalize (anyio scope)")

    def test_anyio_scope_cancel_during_enqueue_leaves_no_half_commit(self):
        """RED at base, GREEN after the fix: the cancel scope is cancelled
        while the enqueue step is pending — the cancellation propagates and
        the settled upload is never half-committed."""
        draft_id = self._run_anyio_step_scenario("enqueue")
        self._assert_no_half_commit(draft_id, "enqueue (anyio scope)")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
