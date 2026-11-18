"""A4d: prove the conftest guard still raises for the four production-shaped
Redis URLs, while letting an unresolvable host through."""
import pytest


@pytest.mark.parametrize(
    "url",
    [
        "redis://127.0.0.1:6379",
        "redis://localhost:6379",
        "redis://redis:6379",
        "redis://knowledgevault_redis:6379",
        "redis://127.0.0.1:6379/0",
        "redis://localhost:6379/0",
    ],
)
def test_guard_blocks_production_shaped_urls(url):
    import tests.conftest as cc

    with pytest.raises(RuntimeError, match="real Redis"):
        cc._reaches_a_real_redis(url) and cc._guarded_from_url(url)


def test_guard_allows_unresolvable_host(monkeypatch):
    import tests.conftest as cc

    assert cc._reaches_a_real_redis("redis://redis.invalid:6379/0") is False
    # the guarded factory lets an unresolvable URL through to the captured
    # original; stub it (restored by monkeypatch) so no client is built
    sentinel = object()
    monkeypatch.setattr(cc, "_orig_from_url", lambda url, *a, **k: sentinel)
    assert cc._guarded_from_url("redis://redis.invalid:6379/0") is sentinel


def test_guard_blocks_sync_from_url_before_calling_original(monkeypatch):
    import tests.conftest as cc

    calls = []
    monkeypatch.setattr(cc, "_orig_from_url", lambda *a, **k: calls.append(a))
    with pytest.raises(RuntimeError, match="real Redis"):
        cc._guarded_from_url("redis://localhost:6379/0")
    assert calls == []


def test_async_guard_blocks_loopback_and_allows_unresolvable(monkeypatch):
    import tests.conftest as cc

    calls = []
    sentinel = object()

    def _stub(url, *a, **k):
        calls.append(url)
        return sentinel

    monkeypatch.setattr(cc, "_orig_async_from_url", _stub)
    with pytest.raises(RuntimeError, match="real Redis"):
        cc._guarded_async_from_url("redis://localhost:6379/0")
    assert calls == []
    assert cc._guarded_async_from_url("redis://redis.invalid:6379/0") is sentinel
    assert calls == ["redis://redis.invalid:6379/0"]


def test_async_guard_without_original_raises_clear_error(monkeypatch):
    import tests.conftest as cc

    monkeypatch.setattr(cc, "_orig_async_from_url", None)
    with pytest.raises(RuntimeError, match="without an original"):
        cc._guarded_async_from_url("redis://redis.invalid:6379/0")


def test_pytest_configure_installed_the_async_guard():
    import redis.asyncio as redis_asyncio

    with pytest.raises(RuntimeError, match="real Redis"):
        redis_asyncio.from_url("redis://localhost:6379/0")


def test_guard_blocks_a_host_resolving_to_loopback():
    """Any hostname that resolves to 127.0.0.1 is blocked, not just the literal."""
    import tests.conftest as cc

    # localhost resolves to loopback; a name that does not resolve is allowed
    assert cc._reaches_a_real_redis("redis://localhost:6379") is True
    assert cc._reaches_a_real_redis("redis://redis.invalid:6379") is False


def test_guard_blocks_with_db_index_and_credentials():
    import tests.conftest as cc

    assert cc._reaches_a_real_redis("redis://:secret@localhost:6379/2") is True
    assert cc._reaches_a_real_redis("rediss://127.0.0.1:6380/0") is True
    assert cc._reaches_a_real_redis("redis://127.0.0.1:6380/0") is True
    assert cc._reaches_a_real_redis("redis://redis.invalid:6380/0") is False
