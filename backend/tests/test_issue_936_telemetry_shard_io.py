"""Issue #936 acceptance checks -- telemetry shard I/O hardening.

Arm's-length checks against the multiprocess shard mechanism in
``app/services/telemetry.py`` (flush observability, torn-write atomicity,
hostile-shard resilience, label injection on the unauthenticated
``/metrics`` exposition, retention pruning, bounded queue history, and
HELP/TYPE lines sourced from the metric constants).

Every check below is phrased "RED at base, GREEN after the fix" per the
issue's ACs 1-7.
"""

import inspect
import json
import logging
import os
import pathlib
import time
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import telemetry as telemetry_module
from app.services.telemetry import (
    METRIC_CHAT_TURNS,
    METRIC_EVALUATOR_FAIL_OPEN,
    METRIC_PROVIDER_CALLS,
    METRIC_QUEUE_DEPTH,
    METRIC_QUEUE_WAIT,
    Telemetry,
    init_telemetry,
    register_metrics_route,
)


def test_flush_failure_warns_and_counts(tmp_path, monkeypatch, caplog):
    """AC1 (issue #936): a shard flush failure must be operator-visible --
    at least one WARNING-level record from ``app.services.telemetry`` AND a
    flush-failure sample with a positive value in the exposition. RED at
    base (the failure is logged at DEBUG only and no flush-failure metric
    family exists), GREEN after the fix."""

    tel = Telemetry(enabled=True, registry_dir=tmp_path)

    def _raising_dumps(payload):
        raise OSError("simulated shard write failure")

    with caplog.at_level(logging.WARNING, logger="app.services.telemetry"):
        # The serializer injection point the module documents for tests
        # (telemetry._dumps) makes the shard write raise inside the same
        # try block the production write_text call lives in.
        with monkeypatch.context() as mp:
            mp.setattr(telemetry_module, "_dumps", _raising_dumps)
            tel.record_chat_turn("t-936-flush-fail")

    warning_records = [
        r
        for r in caplog.records
        if r.name == "app.services.telemetry" and r.levelno >= logging.WARNING
    ]
    assert len(warning_records) >= 1

    text = tel.metrics_text()
    failure_samples = []
    for line in text.splitlines():
        if line.startswith("#") or " " not in line:
            continue
        name_token, _, value_token = line.rpartition(" ")
        if "fail" in name_token.lower():
            try:
                failure_samples.append(float(value_token))
            except ValueError:
                continue
    assert failure_samples and max(failure_samples) >= 1.0


def test_interrupted_write_never_tears_the_previous_shard(tmp_path, monkeypatch):
    """AC2 (issue #936): an interrupted shard write (truncated payload,
    then OSError) must leave the previous good shard readable -- a fresh
    aggregator on the same registry still reads the pre-interruption
    total. RED at base (the truncating in-place write tears the shard and
    the reader silently skips it, so the aggregate shows 0), GREEN after
    the fix (an atomic swap keeps the previous shard intact)."""

    tel = Telemetry(enabled=True, registry_dir=tmp_path)
    for _ in range(3):
        tel.record_chat_turn("t-936-good")

    registry_dir = tmp_path
    real_write_text = pathlib.Path.write_text

    def _torn_write_text(self, data, *args, **kwargs):
        # Tear ONLY writes into the registry directory (the shard or any
        # temp file a fixed implementation may stage there); everything
        # else delegates to the real implementation.
        if self.parent != registry_dir:
            return real_write_text(self, data, *args, **kwargs)
        with open(self, "w", encoding="utf-8") as fh:
            fh.write(data[: len(data) // 2])
        raise OSError("simulated interruption mid-write")

    with monkeypatch.context() as mp:
        mp.setattr(pathlib.Path, "write_text", _torn_write_text)
        # The write-through flush on this record is interrupted mid-write.
        tel.record_chat_turn("t-936-interrupted")

    fresh = Telemetry(enabled=True, registry_dir=tmp_path)
    text = fresh.metrics_text()
    total = None
    for line in text.splitlines():
        if line.startswith("ragapp_chat_turns_total "):
            total = int(float(line.rsplit(" ", 1)[1]))
    assert total is not None, "harness: no chat_turns sample in exposition"
    assert total == 3


def test_malformed_shard_is_skipped_with_warning_and_200(tmp_path, caplog):
    """AC3 (issue #936): a registry holding hostile shards (one invalid
    JSON, one valid JSON with a non-integer chat_turns) alongside a valid
    one must still serve a metrics exposition, with a WARNING recorded per
    skipped shard. RED at base (the unguarded ``int()`` raises out of
    ``metrics_text()`` -- the exact executed-500 signature), GREEN after
    the fix (skip + warn)."""

    valid = tmp_path / "telemetry-11111111-aaaa.json"
    valid.write_text(json.dumps({"chat_turns": 2}), encoding="utf-8")
    invalid_json = tmp_path / "telemetry-22222222-bbbb.json"
    invalid_json.write_text('{"chat_turns": ', encoding="utf-8")
    bad_int = tmp_path / "telemetry-33333333-cccc.json"
    bad_int.write_text(json.dumps({"chat_turns": "not-an-int"}), encoding="utf-8")

    tel = Telemetry(enabled=True, registry_dir=tmp_path)
    with caplog.at_level(logging.WARNING, logger="app.services.telemetry"):
        text = tel.metrics_text()

    assert isinstance(text, str)
    skipped_warnings = [
        r
        for r in caplog.records
        if r.name == "app.services.telemetry" and r.levelno >= logging.WARNING
    ]
    assert len(skipped_warnings) >= 2


def test_unauthenticated_metrics_rejects_injected_reason_label(tmp_path):
    """AC4 (issue #936): a hostile shard that plants a label-breaking
    evaluator_fail_opens reason must not inject arbitrary text into the
    UNAUTHENTICATED ``GET /metrics`` exposition -- the response stays 200
    and the injected reason never reaches the body. RED at base (the raw
    reason is echoed into the exposition), GREEN after the fix (the reason
    is allowlisted/escaped)."""

    hostile_reason = 'injected"} 999\n# '
    shard = tmp_path / "telemetry-98765432-dead.json"
    shard.write_text(
        json.dumps(
            {"chat_turns": 0, "evaluator_fail_opens": {hostile_reason: 5}}
        ),
        encoding="utf-8",
    )

    metrics_app = FastAPI()
    register_metrics_route(metrics_app)
    with patch("app.config.settings.telemetry_enabled", True):
        init_telemetry(registry_dir=tmp_path)
    client = TestClient(metrics_app)
    # Deliberately NO credentials: /metrics is the unauthenticated
    # scrape surface the issue's threat model is about.
    response = client.get("/metrics")

    assert response.status_code == 200
    assert ("injected" in response.text) is False


def test_prune_spares_live_idle_worker_shard(tmp_path):
    """AC5 (issue #936): an idle-but-live worker's shard (mtime older than
    the 3600s retention window) must survive a scraper's aggregation pass
    -- pruning may only reclaim shards of dead processes. RED at base (the
    pruner unlinks it on mtime alone), GREEN after the fix."""

    worker = Telemetry(enabled=True, registry_dir=tmp_path)
    scraper = Telemetry(enabled=True, registry_dir=tmp_path)
    worker.record_chat_turn("t-936-idle")
    worker_shard = tmp_path / worker._shard_name
    assert worker_shard.exists(), "harness: worker shard was never flushed"

    stale = time.time() - 7200
    os.utime(worker_shard, (stale, stale))

    # Aggregation pass; at base the prune unlinks the backdated shard.
    scraper.metrics_text()

    assert worker_shard.exists() is True


def test_queue_history_is_bounded_in_the_flushed_shard(tmp_path):
    """AC6 (issue #936): the queue wait history persisted in a shard must
    be bounded -- after 1000 record_queue_wait calls on one admission
    class, the flushed shard holds at most 200 samples for that class.
    The cap value 200 is the bound issue #936 AC6 specifies (hardcoded
    here on purpose). RED at base (the full 1000-entry list is
    re-serialized on every flush), GREEN after the fix."""

    tel = Telemetry(enabled=True, registry_dir=tmp_path)
    for _ in range(1000):
        tel.record_queue_wait("chat", 0.05, depth=1)

    tel.metrics_text()  # flushes the shard to the registry
    payload = json.loads(
        (tmp_path / tel._shard_name).read_text(encoding="utf-8")
    )
    assert len(payload["queue_waits"]["chat"]) <= 200


def test_help_and_type_lines_come_from_the_metric_constants():
    """AC7 (issue #936, structural): no METRIC_* constant VALUE may appear
    as a string literal inside ``Telemetry.metrics_text``'s source -- the
    HELP/TYPE lines must be built from the constants (f-string
    interpolation) so a metric rename cannot desynchronize the exposition
    header from its samples. RED at base (the HELP/TYPE lines hardcode the
    literal names today), GREEN after the fix."""

    source = inspect.getsource(telemetry_module.Telemetry.metrics_text)
    for literal in (
        METRIC_CHAT_TURNS,
        METRIC_QUEUE_DEPTH,
        METRIC_QUEUE_WAIT,
        METRIC_PROVIDER_CALLS,
        METRIC_EVALUATOR_FAIL_OPEN,
    ):
        assert (literal in source) is False
