import asyncio
from unittest.mock import MagicMock, patch

import pytest

from app.services.draft_job_processor import DraftJobProcessor
from app.services.kms_compile_processor import KMSCompileProcessor
from app.services.wiki_compile_processor import WikiCompileProcessor

PROCESSORS = (
    pytest.param(KMSCompileProcessor, id="KMS"),
    pytest.param(WikiCompileProcessor, id="Wiki"),
    pytest.param(DraftJobProcessor, id="Draft"),
)


@pytest.fixture(autouse=True)
async def _cancel_tasks_created_by_test():
    baseline = asyncio.all_tasks()

    def pending_tasks():
        current = asyncio.current_task()
        live = {task for task in asyncio.all_tasks() if task not in baseline and task is not current}
        return live

    try:
        yield
    finally:
        for _ in range(3):
            live = pending_tasks()
            if not live:
                break
            for task in live:
                task.cancel()
            await asyncio.gather(*live, return_exceptions=True)
        assert not pending_tasks(), "test leaked a live asyncio task"


def _processor(processor_type):
    if processor_type is DraftJobProcessor:
        processor = processor_type(MagicMock(), MagicMock(), MagicMock())
        reset_name = "_recover_on_startup"
    else:
        processor = processor_type(MagicMock())
        reset_name = "_reset_orphans"
    return processor, reset_name


async def _blocked_poll():
    await asyncio.Event().wait()


async def _drain_reset(processor):
    if (reset_task := getattr(processor, "_startup_reset_task", None)) is not None:
        if not reset_task.done():
            reset_task.cancel()
        await asyncio.gather(reset_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("processor_type", PROCESSORS)
async def test_cancelled_start_rolls_back_and_can_retry(processor_type):
    processor, reset_name = _processor(processor_type)
    reset_entered = asyncio.Event()
    release_reset = asyncio.Event()

    async def blocked_to_thread(_fn, *_args, **_kwargs):
        reset_entered.set()
        await release_reset.wait()

    processor._poll_loop = _blocked_poll
    with patch.object(asyncio, "to_thread", side_effect=blocked_to_thread):
        start_task = asyncio.create_task(processor.start())
        await reset_entered.wait()
        start_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start_task
        assert processor._running is False
        assert processor._task is None
        release_reset.set()
        await _drain_reset(processor)

    setattr(processor, reset_name, MagicMock())
    await processor.start()
    try:
        assert processor._running is True
        assert processor._task is not None
    finally:
        await processor.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("processor_type", PROCESSORS)
async def test_reset_failure_rolls_back_and_reraises_before_retry(processor_type):
    processor, reset_name = _processor(processor_type)
    error = RuntimeError("reset failed")
    setattr(processor, reset_name, MagicMock(side_effect=error))

    with pytest.raises(RuntimeError, match="reset failed") as raised:
        await processor.start()
    assert raised.value is error
    assert processor._running is False
    assert processor._task is None

    setattr(processor, reset_name, MagicMock())
    processor._poll_loop = _blocked_poll
    await processor.start()
    try:
        assert processor._running is True
        assert processor._task is not None
    finally:
        await processor.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("processor_type", PROCESSORS)
async def test_reset_task_publication_failure_closes_coro_and_can_retry(processor_type):
    processor, reset_name = _processor(processor_type)
    setattr(processor, reset_name, MagicMock())
    published = []
    error = RuntimeError("reset task publication failed")
    real_create_task = asyncio.create_task

    def fail_first_publication(coro, *_args, **_kwargs):
        published.append(coro)
        if len(published) == 1:
            raise error
        return real_create_task(coro, *_args, **_kwargs)

    with patch.object(asyncio, "create_task", side_effect=fail_first_publication):
        with pytest.raises(RuntimeError, match="reset task publication failed") as raised:
            await processor.start()
    assert raised.value is error
    assert published[0].cr_frame is None
    assert processor._running is False
    assert processor._task is None

    processor._poll_loop = _blocked_poll
    await processor.start()
    try:
        assert processor._task is not None
    finally:
        await processor.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("processor_type", PROCESSORS)
async def test_poll_task_publication_failure_closes_coro_and_can_retry(processor_type):
    processor, reset_name = _processor(processor_type)
    setattr(processor, reset_name, MagicMock())
    processor._poll_loop = _blocked_poll
    published = []
    error = RuntimeError("poll task publication failed")
    real_create_task = asyncio.create_task

    def fail_second_publication(coro, *_args, **_kwargs):
        published.append(coro)
        if len(published) == 2:
            raise error
        return real_create_task(coro, *_args, **_kwargs)

    with patch.object(asyncio, "create_task", side_effect=fail_second_publication):
        with pytest.raises(RuntimeError, match="poll task publication failed") as raised:
            await processor.start()
    assert raised.value is error
    assert len(published) == 2
    assert published[1].cr_frame is None
    assert processor._running is False
    assert processor._task is None

    await processor.start()
    try:
        assert processor._task is not None
    finally:
        await processor.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("processor_type", PROCESSORS)
async def test_reset_finishes_before_poll_task_is_published(processor_type):
    processor, reset_name = _processor(processor_type)
    events = []

    def reset():
        events.append("reset")

    async def poll():
        events.append("poll")
        await asyncio.Event().wait()

    setattr(processor, reset_name, reset)
    processor._poll_loop = poll
    real_create_task = asyncio.create_task

    def record_publication(coro, *_args, **kwargs):
        events.append("publish_reset" if len(events) == 0 else "publish_poll")
        return real_create_task(coro, *_args, **kwargs)

    with patch.object(asyncio, "create_task", side_effect=record_publication):
        await processor.start()
        await asyncio.sleep(0)
    try:
        assert events.index("reset") < events.index("publish_poll") < events.index("poll")
    finally:
        await processor.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("processor_type", PROCESSORS)
async def test_repeated_and_concurrent_starts_publish_one_poll_task(processor_type):
    processor, reset_name = _processor(processor_type)
    reset_entered = asyncio.Event()
    release_reset = asyncio.Event()
    reset_calls = 0

    async def blocked_to_thread(fn, *_args, **_kwargs):
        nonlocal reset_calls
        reset_calls += 1
        reset_entered.set()
        await release_reset.wait()
        fn()

    processor._poll_loop = _blocked_poll
    with patch.object(asyncio, "to_thread", side_effect=blocked_to_thread):
        first = asyncio.create_task(processor.start())
        await reset_entered.wait()
        second = asyncio.create_task(processor.start())
        await asyncio.sleep(0)
        release_reset.set()
        await asyncio.gather(first, second)
    try:
        assert reset_calls == 1
        assert processor._running is True
        assert processor._task is not None
        task_before = processor._task
        await processor.start()
        assert processor._task is task_before
    finally:
        await processor.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("processor_type", PROCESSORS)
async def test_stop_without_retry_prevents_late_publication(processor_type):
    processor, _reset_name = _processor(processor_type)
    reset_entered = asyncio.Event()
    release_reset = asyncio.Event()
    poll_published = False

    async def blocked_to_thread(_fn, *_args, **_kwargs):
        reset_entered.set()
        await release_reset.wait()

    async def poll():
        nonlocal poll_published
        poll_published = True
        await asyncio.Event().wait()

    processor._poll_loop = poll
    start_task = None
    try:
        with patch.object(asyncio, "to_thread", side_effect=blocked_to_thread):
            start_task = asyncio.create_task(processor.start())
            await reset_entered.wait()
            await processor.stop()
            release_reset.set()
            await start_task
        assert processor._running is False
        assert processor._task is None
        assert poll_published is False
    finally:
        release_reset.set()
        if start_task is not None and not start_task.done():
            start_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await start_task
        await processor.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("release_order", ("B-then-A", "A-then-B"))
@pytest.mark.parametrize("processor_type", PROCESSORS)
async def test_stop_retry_shares_reset_and_B_owns_poll(processor_type, release_order):
    processor, _reset_name = _processor(processor_type)
    reset_entered = asyncio.Event()
    release_reset = asyncio.Event()
    reset_calls = 0
    poll_tasks = []
    shield_entered = [asyncio.Event(), asyncio.Event()]
    release_waiter = [asyncio.Event(), asyncio.Event()]
    real_shield = asyncio.shield
    shield_calls = 0

    async def blocked_to_thread(_fn, *_args, **_kwargs):
        nonlocal reset_calls
        reset_calls += 1
        reset_entered.set()
        await release_reset.wait()

    async def poll():
        poll_tasks.append(asyncio.current_task())
        await asyncio.Event().wait()

    def controlled_shield(task):
        nonlocal shield_calls
        index = shield_calls
        shield_calls += 1
        shield_entered[index].set()

        async def wait_for_release():
            await real_shield(task)
            await release_waiter[index].wait()

        return wait_for_release()

    processor._poll_loop = poll
    with (
        patch.object(asyncio, "to_thread", side_effect=blocked_to_thread),
        patch.object(asyncio, "shield", side_effect=controlled_shield),
    ):
        start_a = asyncio.create_task(processor.start())
        await reset_entered.wait()
        await processor.stop()
        start_b = asyncio.create_task(processor.start())
        await shield_entered[1].wait()
        assert reset_calls == 1
        assert not poll_tasks
        release_reset.set()
        if release_order == "B-then-A":
            release_waiter[1].set()
            await start_b
            release_waiter[0].set()
            await start_a
        else:
            release_waiter[0].set()
            await start_a
            release_waiter[1].set()
            await start_b
        await asyncio.sleep(0)
    try:
        assert reset_calls == 1
        assert processor._running is True
        assert processor._task is poll_tasks[0]
        assert len(poll_tasks) == 1
    finally:
        await processor.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("processor_type", PROCESSORS)
async def test_stale_A_cancellation_does_not_clear_B(processor_type):
    processor, _reset_name = _processor(processor_type)
    reset_entered = asyncio.Event()
    release_reset = asyncio.Event()
    poll_tasks = []

    async def blocked_to_thread(_fn, *_args, **_kwargs):
        reset_entered.set()
        await release_reset.wait()

    async def poll():
        poll_tasks.append(asyncio.current_task())
        await asyncio.Event().wait()

    processor._poll_loop = poll
    with patch.object(asyncio, "to_thread", side_effect=blocked_to_thread):
        start_a = asyncio.create_task(processor.start())
        await reset_entered.wait()
        await processor.stop()
        start_b = asyncio.create_task(processor.start())
        start_a.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start_a
        assert processor._running is True
        release_reset.set()
        await start_b
    try:
        assert processor._task is poll_tasks[0]
        assert processor._running is True
    finally:
        await processor.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("processor_type", PROCESSORS)
async def test_shared_reset_failure_only_current_B_rolls_back(processor_type):
    processor, _reset_name = _processor(processor_type)
    reset_entered = asyncio.Event()
    release_reset = asyncio.Event()
    error = RuntimeError("shared reset failed")

    async def blocked_to_thread(_fn, *_args, **_kwargs):
        reset_entered.set()
        await release_reset.wait()
        raise error

    processor._poll_loop = _blocked_poll
    with patch.object(asyncio, "to_thread", side_effect=blocked_to_thread):
        start_a = asyncio.create_task(processor.start())
        await reset_entered.wait()
        await processor.stop()
        start_b = asyncio.create_task(processor.start())
        release_reset.set()
        with pytest.raises(RuntimeError, match="shared reset failed") as raised_b:
            await start_b
        with pytest.raises(RuntimeError, match="shared reset failed") as raised_a:
            await start_a
    assert raised_a.value is raised_b.value
    assert processor._running is False
    assert processor._task is None
    await processor.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("processor_type", PROCESSORS)
async def test_done_reset_task_is_not_reused_after_completion(processor_type):
    processor, reset_name = _processor(processor_type)
    reset = MagicMock()
    setattr(processor, reset_name, reset)
    processor._poll_loop = _blocked_poll
    done_reset = asyncio.create_task(asyncio.sleep(0))
    await done_reset
    processor._startup_reset_task = done_reset

    await processor.start()
    try:
        assert reset.call_count == 1
        assert processor._task is not None
    finally:
        await processor.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("processor_type", PROCESSORS)
async def test_cancelled_reset_task_is_replaced(processor_type):
    processor, reset_name = _processor(processor_type)
    reset = MagicMock()
    setattr(processor, reset_name, reset)
    processor._poll_loop = _blocked_poll
    cancelled = asyncio.create_task(asyncio.sleep(60))
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    processor._startup_reset_task = cancelled
    cancelled.add_done_callback(processor._consume_startup_reset)

    await processor.start()
    try:
        assert reset.call_count == 1
        assert processor._task is not None
    finally:
        await processor.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("processor_type", PROCESSORS)
async def test_start_during_stop_drain_keeps_new_task(processor_type):
    processor, reset_name = _processor(processor_type)
    old_cancelled = asyncio.Event()
    release_drain = asyncio.Event()
    poll_count = 0
    poll_tasks = []

    async def poll():
        nonlocal poll_count
        poll_count += 1
        poll_tasks.append(asyncio.current_task())
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if poll_count == 1:
                old_cancelled.set()
                await release_drain.wait()
            raise

    setattr(processor, reset_name, MagicMock())
    processor._poll_loop = poll
    await processor.start()
    stopping = asyncio.create_task(processor.stop())
    await old_cancelled.wait()
    setattr(processor, reset_name, MagicMock())
    await processor.start()
    assert processor._running is True
    new_task = processor._task
    release_drain.set()
    await stopping
    try:
        assert processor._task is new_task
        assert processor._task is not None
        assert not processor._task.done()
    finally:
        await processor.stop()
