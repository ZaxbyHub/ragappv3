"""A4d: prove the conftest guard still raises for the four production-shaped
Redis URLs, while letting an unresolvable host through."""
import socket

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


def test_guard_allows_unresolvable_host():
    import tests.conftest as cc

    assert cc._reaches_a_real_redis("redis://redis.invalid:6379/0") is False
    # the guarded factory lets an unresolvable URL through to the real
    # redis.from_url; capture the original by installing the guard first
    import redis as _redis_mod

    cc._orig_from_url = _redis_mod.from_url
    client = cc._guarded_from_url("redis://redis.invalid:6379/0")
    assert client is not None


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
