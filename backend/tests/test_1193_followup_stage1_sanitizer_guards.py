"""Supplementary specification tests for the PR #1193 follow-up Stage 1
(groups C, D and E of the collated #1201 + #1202 contract).

Group C (selector ``cancellation_backstop``): the assistant sanitizer must
never run on the event-loop thread — not on the normal paths and not when a
turn's producer task is cancelled mid-generation (the process-shutdown
analogue). Durability is unchanged by that requirement: the cancelled turn
still finalizes as interrupted with the sanitized partial content.

Group D (selector ``container_fence``): ``sanitize_assistant_content`` is
container-aware — it removes ``<answer>``/``</answer>`` wrapper TAGS from
prose and leaves them untouched inside fenced code blocks, including fences
nested in a block quote (lines starting with ``>``) or a list item (lines
starting with ``-``/``*``/``+`` or ``N.``/``N)`` followed by a space). Every
expected output is a fixed point.

Group E (selector ``migration_passes``): the boot-time re-sanitize migration
applies the same sanitizer to every persisted assistant row and must not
rewrite literal markup that lives inside quoted/list fences, while still
cleaning real wrapper tags and staying idempotent.
"""

import ast
import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from app.api.routes import chat as chat_routes  # noqa: E402
from app.models.database import (  # noqa: E402
    get_pool,
    init_db,
    migrate_sanitize_existing_chat_messages,
    run_migrations,
)
from app.utils.assistant_sanitizer import (  # noqa: E402
    cleanup_existing_chat_messages_rows,
    sanitize_assistant_content,
)

# ---------------------------------------------------------------------------
# Group C harness (the direct stream_chat_response drive from
# backend/tests/test_chat_stream_durability.py): real temp database, real
# pool, scripted engine, the producer task taken from _turn_registry().
# ---------------------------------------------------------------------------


def _is_content_event(text: str) -> bool:
    return '"type": "content"' in text or '"type":"content"' in text


async def _await_for(predicate, timeout=5.0, interval=0.02):
    """Async polling: yields to the event loop between polls, which the
    direct-generator tests REQUIRE — the finalize lands while the loop is
    free to run. Bounded; never a fixed sleep."""
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(interval)


async def _cancel_and_await(task):
    """Retire a task this test owns, including on an assertion path."""
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _consume_until(response, predicate):
    """Iterate the real response generator inside a task and cancel the task
    once ``predicate(chunk)`` holds — the exact mechanism Starlette uses on
    client disconnect (task cancellation lands CancelledError at the
    generator's suspension point)."""
    saw = asyncio.Event()

    async def consumer():
        async for chunk in response.body_iterator:
            text = chunk if isinstance(chunk, str) else chunk.decode("utf-8", "replace")
            if predicate(text):
                saw.set()
                break
        # Park on the next __anext__ await so the cancellation lands at the
        # generator's suspension point, not after the loop exited.
        async for _ in response.body_iterator:
            pass

    task = asyncio.create_task(consumer())
    try:
        await asyncio.wait_for(saw.wait(), timeout=10)
        await asyncio.sleep(0)
    finally:
        # ``saw.wait()`` can time out or a later assertion can fail while the
        # reader is parked on the next frame. Always retire the reader task
        # before exposing either outcome to the caller.
        await _cancel_and_await(task)


class _TurnEnv:
    """Per-test environment: real temp DB, real pool, a scripted engine that
    emits one content chunk carrying an answer wrapper tag and then blocks on
    an event that is never set."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="zc1193-stage1-guards-")
        self.db_path = os.path.join(self.tmp, "stream.db")
        init_db(self.db_path)
        run_migrations(self.db_path)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.session_id = self.conn.execute(
            "INSERT INTO chat_sessions (vault_id, user_id, title) VALUES (1, 1, NULL)"
        ).lastrowid
        self.conn.commit()
        self.pool = get_pool(self.db_path)
        self.gate = asyncio.Event()
        self.sanitizer_threads = []

        async def gated_query(*args, **kwargs):
            yield {"type": "content", "content": "<answer>The plan begins "}
            await self.gate.wait()
            yield {"type": "content", "content": "with retrieval."}
            yield {"type": "done", "sources": [], "memories_used": []}

        self.engine = MagicMock()
        self.engine.query = gated_query
        self.engine.llm_client = None

    def rows(self):
        return self.conn.execute(
            "SELECT role, status, turn_id, seq, content FROM chat_messages "
            "WHERE session_id = ? ORDER BY seq, id",
            (self.session_id,),
        ).fetchall()

    def make_stream(self, turn_id):
        """Build the real StreamingResponse for this turn (bypassing only the
        HTTP/auth layers)."""
        return chat_routes.stream_chat_response(
            "What is the plan?",
            [],
            self.engine,
            vault_id=1,
            durable_session_id=self.session_id,
            durable_turn_id=turn_id,
            db_pool=self.pool,
        )

    def close(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


@pytest.fixture
def env():
    e = _TurnEnv()
    try:
        yield e
    finally:
        e.close()


async def _run_cancelled_backstop_turn(env):
    """Install a sanitizer recorder, drive one durable turn whose engine
    blocks after the first content chunk, cancel the producer task, and
    return ``(assistant_row, sanitizer_threads, loop_thread_id)``."""
    original = chat_routes.sanitize_chat_messages_content
    threads = env.sanitizer_threads

    def recording(content):
        threads.append(threading.get_ident())
        return original(content)

    chat_routes.sanitize_chat_messages_content = recording
    loop_thread = threading.get_ident()
    turn_id = "backstop-turn-1"
    response = env.make_stream(turn_id)
    # stream_chat_response creates and registers the producer synchronously;
    # keep the owned task so a failed assertion still retires it.
    entry = chat_routes._turn_registry().get((env.session_id, turn_id))
    assert entry is not None and entry.task is not None
    producer = entry.task
    try:
        await _consume_until(response, _is_content_event)
        # The gate proves the producer is still generating when cancelled.
        assert not producer.done(), "the producer finished before cancellation"
        await _cancel_and_await(producer)
        assert await _await_for(
            lambda: len([r for r in env.rows() if r[0] == "assistant"]) == 1
        )
        assistant_rows = [r for r in env.rows() if r[0] == "assistant"]
        assert len(assistant_rows) == 1
        return assistant_rows[0], threads, loop_thread
    finally:
        chat_routes.sanitize_chat_messages_content = original
        await _cancel_and_await(producer)


async def test_cancellation_backstop_finalizes_interrupted_turn_off_the_event_loop(env):
    """A turn whose producer is cancelled mid-generation still finalizes as
    interrupted with the sanitized partial content (the wrapper tag is gone,
    the words remain), and every sanitizer invocation for that finalize runs
    off the event-loop thread."""
    assistant_row, threads, loop_thread = await _run_cancelled_backstop_turn(env)
    status, content = assistant_row[1], assistant_row[4]
    assert status == "interrupted", assistant_row
    assert "<answer>" not in content, content
    assert "The plan begins" in content, content
    assert threads, "the sanitizer never ran for the cancelled turn"
    for thread_id in threads:
        assert thread_id != loop_thread, (
            f"sanitizer ran on the event-loop thread (tid {thread_id})"
        )


async def test_cancellation_backstop_sanitizer_invoked_at_least_once(env):
    """Guards the backstop thread assertion against a vacuous pass: the
    cancelled turn's finalize path invokes the persistence-boundary
    sanitizer at least once."""
    _assistant_row, threads, _loop_thread = await _run_cancelled_backstop_turn(env)
    assert threads, "the sanitizer never ran for the cancelled turn"


async def test_cancellation_backstop_submission_does_not_block_loop(env):
    """A slow finalize must not occupy the loop during cancellation.

    The finalize helper is patched to sleep; the producer is cancelled;
    the cancellation must complete well before the worker finishes.
    An inline sync backstop (the base behaviour) blocks here and fails.
    """
    original = chat_routes._finalize_durable_turn_sync
    calls = []
    started_event = threading.Event()

    def slow(db_pool, turn_state, status):
        calls.append(threading.get_ident())
        started_event.set()
        time.sleep(0.4)
        return original(db_pool, turn_state, status)

    chat_routes._finalize_durable_turn_sync = slow
    loop_thread = threading.get_ident()
    turn_id = "backstop-responsiveness-1"
    response = env.make_stream(turn_id)
    entry = chat_routes._turn_registry().get((env.session_id, turn_id))
    assert entry is not None and entry.task is not None
    producer = entry.task
    try:
        await _consume_until(response, _is_content_event)
        assert not producer.done()
        started = time.monotonic()
        await _cancel_and_await(producer)
        elapsed = time.monotonic() - started
        # Observe worker start only AFTER measuring responsiveness.
        for _ in range(250):
            if started_event.is_set():
                break
            await asyncio.sleep(0.01)
        assert started_event.is_set(), "the finalize helper never ran"
        assert elapsed < 0.2, f"cancellation blocked {elapsed:.3f}s on the finalize"
        assert all(t != loop_thread for t in calls), "finalize ran on the loop thread"
    finally:
        drain = getattr(chat_routes, "_drain_turn_finalize_executor", None)
        if drain is not None:
            drain(timeout=5.0)
        chat_routes._finalize_durable_turn_sync = original
        await _cancel_and_await(producer)


async def test_cancellation_backstop_no_inline_fallback_when_worker_unavailable(
    env, caplog
):
    """If the off-loop worker cannot be created the finalize is logged
    and skipped; it must never fall back to an inline sanitizer call
    on the event loop (issue #1202 AC1).
    """
    loop_thread = threading.get_ident()
    original_factory = getattr(chat_routes, "_get_turn_finalize_executor", None)
    threads = env.sanitizer_threads
    recorder = chat_routes.sanitize_chat_messages_content

    def recording(content):
        threads.append(threading.get_ident())
        return recorder(content)

    def boom():
        raise RuntimeError("no worker thread available")

    chat_routes.sanitize_chat_messages_content = recording
    setattr(chat_routes, "_get_turn_finalize_executor", boom)
    turn_id = "backstop-no-worker-1"
    response = env.make_stream(turn_id)
    entry = chat_routes._turn_registry().get((env.session_id, turn_id))
    assert entry is not None and entry.task is not None
    producer = entry.task
    try:
        await _consume_until(response, _is_content_event)
        await _cancel_and_await(producer)
        await asyncio.sleep(0.1)
        assert not threads, (
            f"inline fallback sanitized on the loop thread: {threads} (loop {loop_thread})"
        )
        assert not [r for r in env.rows() if r[0] == "assistant"], (
            "a no-worker skip must not claim a persisted assistant row"
        )
        submit = getattr(chat_routes, "_submit_durable_turn_finalize", None)
        assert callable(submit), "no off-loop submission helper"
        state = {"finalized": False}
        assert submit(env.pool, state, "interrupted") is False, (
            "a missing worker must not report successful submission"
        )
        assert state["finalized"] is False, (
            "a missing worker must not transfer finalization ownership"
        )
        assert "worker unavailable" in caplog.text, (
            "the no-worker skip must be logged, not silent"
        )
    finally:
        if original_factory is None:
            delattr(chat_routes, "_get_turn_finalize_executor")
        else:
            setattr(chat_routes, "_get_turn_finalize_executor", original_factory)
        chat_routes.sanitize_chat_messages_content = recorder
        await _cancel_and_await(producer)


# ---------------------------------------------------------------------------
# Shutdown ordering: the finalize worker must be drained before the pool closes.
# ---------------------------------------------------------------------------


def test_cancellation_backstop_lifespan_calls_drain_before_pool_close():
    """Pin an actual CALL node inside `lifespan`'s SHUTDOWN region (after its
    yield): a drain in startup or in another function must not satisfy this.
    """
    lifespan_path = Path(__file__).resolve().parents[1] / "app" / "lifespan.py"
    tree = ast.parse(lifespan_path.read_text(encoding="utf-8"))
    lifespan = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan"
    )
    yield_lines = [n.lineno for n in ast.walk(lifespan) if isinstance(n, ast.Yield)]
    assert yield_lines, "lifespan has no yield"
    shutdown_after = max(yield_lines)

    def shutdown_calls(predicate):
        return [
            node.lineno
            for node in ast.walk(lifespan)
            if isinstance(node, ast.Call)
            and node.lineno > shutdown_after
            and predicate(node)
        ]

    drain_lines = shutdown_calls(
        lambda n: (
            isinstance(n.func, ast.Name)
            and n.func.id == "_drain_turn_finalize_executor"
        )
    )
    close_lines = shutdown_calls(
        lambda n: (
            isinstance(n.func, ast.Attribute)
            and n.func.attr == "close_all"
            and isinstance(n.func.value, ast.Attribute)
            and n.func.value.attr == "db_pool"
        )
    )
    assert drain_lines, "lifespan shutdown never CALLS the finalize drain"
    assert close_lines, "lifespan shutdown no longer closes the DB pool"
    assert min(drain_lines) < min(close_lines), (
        "the pool is closed before the worker is drained"
    )


async def test_cancellation_backstop_drain_commits_before_pool_close(env):
    """A queued/slow finalize is drained before the pool becomes unusable,
    and the interrupted row is committed by that drain.
    """
    drain = getattr(chat_routes, "_drain_turn_finalize_executor", None)
    assert callable(drain), "no shutdown drain for the finalize worker"
    original = chat_routes._finalize_durable_turn_sync
    worker_started = threading.Event()

    def slow(db_pool, turn_state, status):
        worker_started.set()
        time.sleep(0.25)
        return original(db_pool, turn_state, status)

    chat_routes._finalize_durable_turn_sync = slow
    turn_id = "backstop-drain-1"
    response = env.make_stream(turn_id)
    entry = chat_routes._turn_registry().get((env.session_id, turn_id))
    assert entry is not None and entry.task is not None
    producer = entry.task
    try:
        await _consume_until(response, _is_content_event)
        await _cancel_and_await(producer)
        for _ in range(250):
            if worker_started.is_set():
                break
            await asyncio.sleep(0.01)
        assert worker_started.is_set(), "the finalize worker never started"
        drain(timeout=5.0)
        rows = [r for r in env.rows() if r[0] == "assistant"]
        assert len(rows) == 1, f"drain did not commit the interrupted row: {rows}"
        assert rows[0][1] == "interrupted", rows[0]
        assert "<answer>" not in rows[0][4], rows[0]
    finally:
        chat_routes._finalize_durable_turn_sync = original
        await _cancel_and_await(producer)


# ---------------------------------------------------------------------------
# Group D: container-aware fences (pure function).
# ---------------------------------------------------------------------------


class ContainerFenceSanitizerTest(unittest.TestCase):
    """A fence nested in a block quote or a list item protects literal
    wrapper tags; every expected output is a fixed point."""

    def _assert_unchanged(self, raw):
        once = sanitize_assistant_content(raw)
        self.assertEqual(once, raw)
        self.assertEqual(sanitize_assistant_content(once), once)

    def _assert_becomes(self, raw, expected):
        once = sanitize_assistant_content(raw)
        self.assertEqual(once, expected)
        self.assertEqual(sanitize_assistant_content(once), once)

    def test_container_fence_quote_indent_opener_unchanged(self):
        self._assert_unchanged(">   ```xml\n<answer>keep me</answer>\n>   ```")

    def test_container_fence_nested_quotes_unchanged(self):
        self._assert_unchanged(">> ```\n<answer>keep me</answer>\n>> ```")

    def test_container_fence_quote_mismatch_not_closed_by_bare_fence(self):
        # A quoted opener is NOT closed by a later unquoted bare fence line,
        # so protected content is never released early.
        self._assert_unchanged("> ```\ncode\n```\n<answer>keep me</answer>")

    def test_container_fence_crlf_inside_quoted_fence_unchanged(self):
        self._assert_unchanged("> ```xml\r\n<answer>keep me</answer>\r\n> ```")

    def test_container_fence_list_fences_unchanged(self):
        for raw in (
            "- ```xml\n  <answer>keep me</answer>\n  ```",
            "* ~~~\n  <answer>keep me</answer>\n  ~~~",
            "1. ```\n   <answer>keep me</answer>\n   ```",
            "+ ```\n    <answer>keep me</answer>\n    ```",
        ):
            with self.subTest(raw=raw):
                self._assert_unchanged(raw)

    def test_container_fence_list_fence_close_then_real_wrapper_stripped(self):
        # A list fence is closed by the indented fence line inside the item,
        # and a real wrapper that follows on its own line is still stripped.
        self._assert_becomes(
            "- ```\n  code\n  ```\n<answer>real</answer>",
            "- ```\n  code\n  ```\nreal",
        )

    def test_container_fence_list_fence_not_closed_by_unindented_line(self):
        # A list fence is NOT closed by an unindented bare fence line at
        # column 0 (that line is outside the item).
        self._assert_unchanged("- ```\ncode\n```\n<answer>real</answer>")

    def test_container_fence_mixed_case_attributed_wrapper_unchanged(self):
        self._assert_unchanged('> ```\n<AnSwEr style="x">keep me</AnSwEr>\n> ```')

    def test_container_fence_invalid_opener_in_quote_still_strips(self):
        # The info string contains a backtick, so the quoted line is not a
        # fence and does not shield the wrapper tag that follows.
        self._assert_becomes(
            "> ```a`b\n<answer>strip me</answer>",
            "> ```a`b\nstrip me",
        )

    def test_container_fence_quote_and_list_opener_line_unchanged(self):
        self._assert_unchanged("> - ```\n>   <answer>keep me</answer>\n>   ```")

    def test_container_fence_wrapper_sharing_quoted_opener_line(self):
        # A wrapper tag sharing the quoted opener line is stripped, the
        # fence content kept.
        self._assert_becomes("> <answer>```\ncode\n> ```", "> ```\ncode\n> ```")

    def test_container_fence_wrapper_before_container_prefix_unchanged(self):
        # Wrapper tags and container markers may be interleaved: the scanner
        # must strip the wrapper but keep the fenced literal either way.
        for raw, expected in (
            (
                "<answer>> ```\n<answer>keep me</answer>\n> ```\n</answer>",
                "> ```\n<answer>keep me</answer>\n> ```",
            ),
            (
                "<answer>- ```\n  <answer>keep me</answer>\n  ```\n</answer>",
                "- ```\n  <answer>keep me</answer>\n  ```",
            ),
        ):
            with self.subTest(raw=raw):
                self._assert_becomes(raw, expected)

    def test_container_fence_plain_fence_baseline_unchanged(self):
        # Plain-fence behaviour is unchanged (GREEN at base): a bare fenced
        # block protects its literal tag, and a fence indented four spaces is
        # NOT an opener, so its wrapper tag is still stripped.
        self._assert_unchanged("```\n<answer>keep</answer>\n```")
        once = sanitize_assistant_content("    ```\n<answer>strip me</answer>")
        # The sanitizer trims the ends of the whole text (its documented
        # contract), so the returned text is the trimmed fixed point; the
        # discriminating behaviour is that the tag was stripped at all
        # (the indented line did not open a protecting fence).
        self.assertEqual(once, "```\nstrip me")
        self.assertIn("strip me", once)
        self.assertNotIn("<answer>", once)
        self.assertEqual(sanitize_assistant_content(once), once)


# ---------------------------------------------------------------------------
# Group E: the boot-time re-sanitize migration.
# ---------------------------------------------------------------------------


class MigrationPassesTest(unittest.TestCase):
    """migrate_sanitize_existing_chat_messages rewrites only rows whose
    content changes: quoted/list-fence literal markup survives, real wrapper
    tags are cleaned, user rows are untouched, and a second run is a no-op."""

    def setUp(self) -> None:
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        init_db(self.path)
        run_migrations(self.path)
        self.addCleanup(self._remove_db)

    def _remove_db(self) -> None:
        try:
            os.remove(self.path)
        except OSError:
            pass

    def _seed(self, *rows: tuple[str, str]) -> list[int]:
        ids: list[int] = []
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO chat_sessions (id, vault_id) VALUES (1, 1)")
            for role, content in rows:
                cursor = conn.execute(
                    "INSERT INTO chat_messages (session_id, role, content)"
                    " VALUES (?, ?, ?)",
                    (1, role, content),
                )
                ids.append(cursor.lastrowid)
            conn.commit()
        return ids

    def _contents(self) -> dict[int, str]:
        with sqlite3.connect(self.path) as conn:
            return {
                row[0]: row[1]
                for row in conn.execute("SELECT id, content FROM chat_messages")
            }

    def test_migration_passes_quoted_fence_row_unchanged(self):
        # The literal markup inside the quoted fence survives the migration.
        raw = "> ```xml\n<answer>keep me</answer>\n> ```"
        (row_id,) = self._seed(("assistant", raw))
        migrate_sanitize_existing_chat_messages(self.path)
        self.assertEqual(self._contents()[row_id], raw)

    def test_migration_passes_list_fence_row_unchanged(self):
        raw = "- ```\n  <answer>keep</answer>\n  ```"
        (row_id,) = self._seed(("assistant", raw))
        migrate_sanitize_existing_chat_messages(self.path)
        self.assertEqual(self._contents()[row_id], raw)

    def test_migration_passes_real_wrapper_row_cleaned(self):
        (row_id,) = self._seed(("assistant", "<answer>real</answer>"))
        migrate_sanitize_existing_chat_messages(self.path)
        self.assertEqual(self._contents()[row_id], "real")

    def test_migration_passes_wrapper_before_container_prefix_unchanged(self):
        raw = "<answer>> ```\n<answer>keep me</answer>\n> ```\n</answer>"
        ids = self._seed(("assistant", raw), ("assistant", raw))
        migrate_sanitize_existing_chat_messages(self.path)
        contents = self._contents()
        self.assertEqual(
            [contents[i] for i in ids],
            ["> ```\n<answer>keep me</answer>\n> ```"] * 2,
            contents,
        )

    def test_migration_passes_user_row_untouched(self):
        user_row, assistant_row = self._seed(
            ("user", "<answer>x</answer>"),
            ("assistant", "<answer>z</answer>"),
        )
        migrate_sanitize_existing_chat_messages(self.path)
        contents = self._contents()
        self.assertEqual(contents[user_row], "<answer>x</answer>")
        # The sibling assistant row IS rewritten — proves the migration ran.
        self.assertEqual(contents[assistant_row], "z")

    def test_migration_passes_second_run_is_noop(self):
        self._seed(
            ("assistant", "> ```xml\n<answer>keep me</answer>\n> ```"),
            ("assistant", "- ```\n  <answer>keep</answer>\n  ```"),
            ("assistant", "<answer>real</answer>"),
        )
        migrate_sanitize_existing_chat_messages(self.path)
        first = self._contents()
        migrate_sanitize_existing_chat_messages(self.path)
        second = self._contents()
        self.assertEqual(first, second)

    def test_migration_passes_cleanup_helper_omits_clean_rows(self):
        entries = cleanup_existing_chat_messages_rows(
            [(1, "Already clean."), (2, "real"), (3, "```\nplain\n```")]
        )
        self.assertEqual(entries, [])
        # Guard against a vacuous pass: the helper still reports rows whose
        # content actually changes.
        self.assertEqual(
            cleanup_existing_chat_messages_rows([(4, "<answer>dirty</answer>")]),
            [(4, "dirty")],
        )


if __name__ == "__main__":
    unittest.main()
