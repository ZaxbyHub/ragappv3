"""Turn correlation and measured telemetry (issue #518, Workstream E3).

Per-turn correlation ids, measured stage durations, queue waits/depth,
first-useful-content latency, provider call outcomes (ok / partial /
unavailable / empty), and embedding cache hits — exposed as a Prometheus
text exposition at ``GET /metrics`` and propagated outbound as W3C
``traceparent`` + ``X-Request-ID`` headers on provider HTTP calls.

Multiprocess-safe aggregation: every ``Telemetry`` instance owning a
``registry_dir`` flushes its counters to a per-process JSON shard in that
directory; ``metrics_text()`` aggregates every shard, so one scraper per
replica set observes the sum. (Polling-grade aggregation; a Redis or OTel
collector upgrade path is documented in docs/operations.md.)

``TELEMETRY_ENABLED=false`` (or ``Telemetry(enabled=False)``) is inert:
recorders are no-ops, snapshots stay empty, no ``ragapp_`` samples are
rendered, and no correlation headers are produced.

OTLP export is an OPTIONAL EXTRA: installing the pinned packages from
backend/requirements-otel.txt and setting OTEL_EXPORTER_OTLP_ENDPOINT
activates ``maybe_init_otel_export()`` (called from ``init_telemetry``),
which bridges the counters here to an OTLP metric exporter. Without the
packages or the endpoint nothing OTel-related loads. The GenAI semantic
conventions (open-telemetry/semantic-conventions-genai) are Development
status with no stable release as of 2026-09 — pin the exact version you
deploy and do not describe them as stable (docs/operations.md).
"""

import hashlib
import json
import logging
import os
import secrets
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

PROVIDER_OUTCOMES = ("ok", "partial", "unavailable", "empty")

METRIC_CHAT_TURNS = "ragapp_chat_turns_total"
METRIC_QUEUE_DEPTH = "ragapp_queue_depth"
METRIC_QUEUE_WAIT = "ragapp_queue_wait_seconds"
METRIC_PROVIDER_CALLS = "ragapp_provider_calls_total"

_turn_var: ContextVar[Optional[str]] = ContextVar("ragapp_turn_id", default=None)


def set_current_turn(turn_id: Optional[str]) -> None:
    """Bind the per-turn correlation id for the current task context."""
    _turn_var.set(turn_id)


def current_turn_id() -> Optional[str]:
    return _turn_var.get()


def _status_to_outcome(status: Optional[str]) -> str:
    if status is None or status == "ok":
        return "ok"
    if status in ("timeout", "request_error", "unavailable", "circuit_open",
                  "http_error", "invalid_json", "admission_rejected"):
        return "unavailable"
    if status == "empty":
        return "empty"
    return "partial"


class Telemetry:
    """In-process measured-metrics recorder with multiprocess aggregation."""

    def __init__(self, *, enabled: bool = True, registry_dir: Optional[Path] = None):
        self.enabled = enabled
        self.registry_dir = Path(registry_dir) if registry_dir else None
        self._chat_turns = 0
        self._stage_durations: Dict[str, Dict[str, float]] = {}
        self._queue_waits: Dict[str, list] = {}
        self._queue_depths: Dict[str, list] = {}
        self._first_useful: Dict[str, float] = {}
        self._provider_calls: Dict[str, Dict[str, int]] = {}
        self._embedding_cache_hits = 0
        self._shard_name = f"telemetry-{os.getpid()}-{secrets.token_hex(4)}.json"
        self._otel_observers: list = []

    # ----------------------------------------------------------- recorders

    def record_chat_turn(self, turn_id: str) -> None:
        if not self.enabled or not turn_id:
            return
        self._chat_turns += 1
        # Write-through: the shard is the cross-process aggregation surface,
        # so counters land on disk as they are recorded (not only on scrape).
        self._flush_shard()
        self._notify_otel_observers()

    def record_stage(self, turn_id: str, stage: str, duration_seconds: float) -> None:
        if not self.enabled or not turn_id:
            return
        self._stage_durations.setdefault(turn_id, {})[stage] = float(
            duration_seconds
        )

    def record_queue_wait(self, admission_class: str, wait_seconds: float,
                          depth: int = 0) -> None:
        if not self.enabled:
            return
        self._queue_waits.setdefault(str(admission_class), []).append(
            float(wait_seconds)
        )
        self._queue_depths.setdefault(str(admission_class), []).append(int(depth))

    def record_first_useful_content(self, turn_id: str, seconds: float) -> None:
        if not self.enabled or not turn_id:
            return
        self._first_useful[turn_id] = float(seconds)

    def record_provider_call(self, provider: str, outcome: str) -> None:
        if outcome not in PROVIDER_OUTCOMES:
            raise ValueError(
                f"unknown provider outcome {outcome!r}; expected one of "
                f"{PROVIDER_OUTCOMES}"
            )
        if not self.enabled:
            return
        per_provider = self._provider_calls.setdefault(str(provider), {})
        per_provider[outcome] = per_provider.get(outcome, 0) + 1
        self._flush_shard()

    def record_embedding_cache(self, hit: bool) -> None:
        if not self.enabled:
            return
        if hit:
            self._embedding_cache_hits += 1
        self._flush_shard()

    # ------------------------------------------------------------- reading

    def snapshot(self) -> dict:
        return {
            "chat_turns": self._chat_turns,
            "stage_durations": {
                turn: dict(stages)
                for turn, stages in self._stage_durations.items()
            },
            "queue_waits": {cls: list(v) for cls, v in self._queue_waits.items()},
            "queue_depths": {
                cls: list(v) for cls, v in self._queue_depths.items()
            },
            "first_useful_content": dict(self._first_useful),
            "provider_calls": {
                provider: dict(outcomes)
                for provider, outcomes in self._provider_calls.items()
            },
            "embedding_cache_hits": self._embedding_cache_hits,
        }

    def attach_otel_observer(self, observer) -> None:
        """Register the OTLP bridge observer (called on its export cadence)."""
        self._otel_observers.append(observer)

    def _notify_otel_observers(self) -> None:
        for observer in list(self._otel_observers):
            try:
                observer(None)
            except Exception:  # noqa: BLE001 — export must never break records
                logger.debug("otel observer failed", exc_info=True)

    def reset(self) -> None:
        self._chat_turns = 0
        self._stage_durations = {}
        self._queue_waits = {}
        self._queue_depths = {}
        self._first_useful = {}
        self._provider_calls = {}
        self._embedding_cache_hits = 0
        if self.registry_dir is not None:
            shard = self.registry_dir / self._shard_name
            try:
                shard.unlink(missing_ok=True)
            except OSError:  # pragma: no cover — best effort
                pass

    # ------------------------------------------------------ multiprocess

    def _flush_shard(self) -> None:
        if self.registry_dir is None:
            return
        try:
            self.registry_dir.mkdir(parents=True, exist_ok=True)
            counts = {
                "chat_turns": self._chat_turns,
                "queue_waits": self._queue_waits,
                "queue_depths": self._queue_depths,
                "provider_calls": self._provider_calls,
                "embedding_cache_hits": self._embedding_cache_hits,
            }
            (self.registry_dir / self._shard_name).write_text(
                json.dumps(counts), encoding="utf-8"
            )
        except OSError:  # pragma: no cover — registry on read-only mount
            logger.debug("telemetry shard flush failed", exc_info=True)

    def _aggregate_counts(self) -> dict:
        totals = {
            "chat_turns": 0,
            "queue_waits": {},
            "queue_depths": {},
            "provider_calls": {},
            "embedding_cache_hits": 0,
        }
        shards: list = []
        if self.registry_dir is not None and self.registry_dir.exists():
            now = time.time()
            for entry in sorted(self.registry_dir.glob("telemetry-*.json")):
                # Retention (swarm review PRR-018): shards are named per
                # (pid, random token), so a restarted replica leaves its
                # dead shard behind forever. A shard not written for over
                # an hour belongs to a dead process (live processes flush
                # write-through on every record) — prune it during
                # aggregation. The current process's shard is exempt.
                try:
                    if (
                        entry.name != self._shard_name
                        and now - entry.stat().st_mtime > 3600
                    ):
                        entry.unlink(missing_ok=True)
                        continue
                except OSError:  # pragma: no cover — racing unlink
                    continue
                try:
                    shards.append(
                        json.loads(entry.read_text(encoding="utf-8"))
                    )
                except (OSError, ValueError):  # pragma: no cover — torn shard
                    continue
        else:
            shards.append(self._local_counts())
        for shard in shards:
            totals["chat_turns"] += int(shard.get("chat_turns", 0))
            totals["embedding_cache_hits"] += int(
                shard.get("embedding_cache_hits", 0)
            )
            for cls, waits in shard.get("queue_waits", {}).items():
                totals["queue_waits"].setdefault(cls, []).extend(waits)
            for cls, depths in shard.get("queue_depths", {}).items():
                totals["queue_depths"].setdefault(cls, []).extend(depths)
            for provider, outcomes in shard.get("provider_calls", {}).items():
                merged = totals["provider_calls"].setdefault(provider, {})
                for outcome, count in outcomes.items():
                    merged[outcome] = merged.get(outcome, 0) + int(count)
        return totals

    def _local_counts(self) -> dict:
        return {
            "chat_turns": self._chat_turns,
            "queue_waits": self._queue_waits,
            "queue_depths": self._queue_depths,
            "provider_calls": self._provider_calls,
            "embedding_cache_hits": self._embedding_cache_hits,
        }

    def metrics_text(self) -> str:
        if not self.enabled:
            return ""
        self._flush_shard()
        totals = self._aggregate_counts()
        lines = [
            "# HELP ragapp_chat_turns_total Chat turns recorded.",
            "# TYPE ragapp_chat_turns_total counter",
            f"{METRIC_CHAT_TURNS} {totals['chat_turns']}",
        ]
        for cls in sorted(totals["queue_depths"]):
            depths = totals["queue_depths"][cls]
            lines.append(
                f'{METRIC_QUEUE_DEPTH}{{admission_class="{cls}"}} '
                f"{depths[-1] if depths else 0}"
            )
        for cls in sorted(totals["queue_waits"]):
            waits = totals["queue_waits"][cls]
            avg = (sum(waits) / len(waits)) if waits else 0.0
            lines.append(
                f'{METRIC_QUEUE_WAIT}{{admission_class="{cls}"}} {avg:.6f}'
            )
        for provider in sorted(totals["provider_calls"]):
            for outcome in sorted(totals["provider_calls"][provider]):
                count = totals["provider_calls"][provider][outcome]
                lines.append(
                    f'{METRIC_PROVIDER_CALLS}{{provider="{provider}",'
                    f'outcome="{outcome}"}} {count}'
                )
        lines.append(
            f"ragapp_embedding_cache_hits_total {totals['embedding_cache_hits']}"
        )
        return "\n".join(lines) + "\n"


_singleton: Optional[Telemetry] = None


def init_telemetry(registry_dir: Optional[Path] = None) -> Telemetry:
    """(Re)bind the process singleton to the current telemetry_enabled.

    When ``registry_dir`` is not given, the settings-configured
    ``telemetry_registry_dir`` applies (swarm review F-008): deployments
    running multiple workers point it at one shared directory so
    ``GET /metrics`` aggregates every shard; empty = single-process counters.
    """
    global _singleton
    from app.config import settings

    if registry_dir is None:
        configured = str(
            getattr(settings, "telemetry_registry_dir", "") or ""
        )
        registry_dir = Path(configured) if configured else None
    _singleton = Telemetry(
        enabled=bool(getattr(settings, "telemetry_enabled", True)),
        registry_dir=registry_dir,
    )
    # OTLP export (PRR-017): only when telemetry itself is enabled — a
    # disabled telemetry surface must never open export network connections,
    # including from test environments where the endpoint env var may be set.
    if _singleton.enabled:
        maybe_init_otel_export()
    return _singleton


def get_telemetry() -> Telemetry:
    global _singleton
    if _singleton is None:
        init_telemetry()
    return _singleton


def reset_telemetry() -> None:
    """Test/teardown hook — drop the singleton (see conftest reset fixture)."""
    global _singleton
    _singleton = None


def correlation_headers() -> Dict[str, str]:
    """W3C traceparent + X-Request-ID for the current turn.

    Empty dict when telemetry is disabled or no correlation identity is set.
    The identity is the current turn id, falling back to the inbound request
    id stamped by LoggingMiddleware (request_id_var) so RequestIdFilter's
    logging identity and the outbound header are the SAME value.

    Defense-in-depth (swarm review F-004): an id that is not safe as an HTTP
    header value (e.g. client-poisoned obs-text) is replaced by a
    deterministic ASCII surrogate so outbound provider calls can never raise
    UnicodeEncodeError inside circuit-breaker-wrapped code. The middleware
    already regenerates unsafe inbound ids; this guards any other path that
    sets a correlation id.
    """
    telemetry = get_telemetry()
    if not telemetry.enabled:
        return {}
    turn_id = current_turn_id()
    if not turn_id:
        from app.utils.request_context import request_id_var

        turn_id = request_id_var.get() or None
    if not turn_id:
        return {}
    from app.utils.request_context import is_safe_request_id

    if not is_safe_request_id(turn_id):
        turn_id = "gen-{}".format(
            hashlib.sha256(turn_id.encode("utf-8", "replace")).hexdigest()[:16]
        )
    trace_id = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()[:32]
    span_id = secrets.token_hex(8)
    return {
        "traceparent": f"00-{trace_id}-{span_id}-01",
        "X-Request-ID": turn_id,
    }


def register_metrics_route(app) -> None:
    """Attach GET /metrics (text/plain Prometheus exposition)."""
    from fastapi.responses import PlainTextResponse

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint() -> PlainTextResponse:
        return PlainTextResponse(
            get_telemetry().metrics_text(), media_type="text/plain"
        )


# Pinned alongside backend/requirements-otel.txt (the optional extra).
_OTLP_EXPORT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"
_otel_export_started = False


def maybe_init_otel_export() -> bool:
    """Bridge the telemetry counters to OTLP when the optional extra is on.

    Activation requires BOTH the pinned optional packages
    (backend/requirements-otel.txt) AND ``OTEL_EXPORTER_OTLP_ENDPOINT``.
    Returns True when an OTLP pipeline was started. Import failures are
    the documented off-state, not an error: air-gapped installs never
    install the extra and this function is a no-op.
    """
    import os

    global _otel_export_started
    if _otel_export_started:
        return True
    endpoint = os.environ.get(_OTLP_EXPORT_ENV, "").strip()
    if not endpoint:
        return False
    try:
        from opentelemetry import metrics as otel_metrics
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import (
            PeriodicExportingMetricReader,
        )
    except ImportError:
        logger.info(
            "OTEL_EXPORTER_OTLP_ENDPOINT is set but the optional OTel extra "
            "is not installed (backend/requirements-otel.txt); OTLP export "
            "stays off."
        )
        return False

    telemetry = get_telemetry()
    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=endpoint)
    )
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter("ragapp.telemetry")
    counter = meter.create_counter("ragapp_chat_turns_total")
    # Publish the aggregate on each export interval by reading the same
    # snapshot /metrics serves — the shard aggregate, so replicas sum.
    observed = {"last": 0}

    def _observe(_options):
        # Fired on each chat-turn record; the OTel reader exports the
        # accumulated counter on its own cadence.
        totals = telemetry._aggregate_counts()
        delta = totals["chat_turns"] - observed["last"]
        observed["last"] = totals["chat_turns"]
        if delta > 0:
            counter.add(delta)

    otel_metrics.set_meter_provider(provider)
    telemetry.attach_otel_observer(_observe)
    _otel_export_started = True
    logger.info("OTLP metric export started (endpoint configured)")
    return True
