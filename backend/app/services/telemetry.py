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
import math
import os
import secrets
import socket
import threading
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

PROVIDER_OUTCOMES = ("ok", "partial", "unavailable", "empty")

# Module-local alias for the shard serializer. Tests patch THIS, not
# ``json.dumps`` — telemetry imports the stdlib module, so patching
# ``telemetry.json.dumps`` would mutate json for the whole process.
_dumps = json.dumps

METRIC_CHAT_TURNS = "ragapp_chat_turns_total"
METRIC_QUEUE_DEPTH = "ragapp_queue_depth"
METRIC_QUEUE_WAIT = "ragapp_queue_wait_seconds"
METRIC_PROVIDER_CALLS = "ragapp_provider_calls_total"
METRIC_EVALUATOR_FAIL_OPEN = "ragapp_retrieval_evaluator_fail_open_total"
METRIC_EMBEDDING_CACHE_HITS = "ragapp_embedding_cache_hits_total"
METRIC_SHARD_FLUSH_FAILURES = "ragapp_telemetry_shard_flush_failures_total"

# Per-class cap on retained queue-wait/queue-depth history (issue #936 AC6):
# the lists are re-serialized in full on every write-through flush, so an
# unbounded history grows shard size and lock-hold time with process
# lifetime. 200 samples per class keeps percentiles honest while bounding
# the serialized blob.
QUEUE_HISTORY_CAP = 200

# Rate-limit window (seconds, monotonic) for operator-facing WARNINGs about
# shard flush failures and skipped shards (issue #936 AC1/AC3): a persistent
# failure must be visible but must not flood the log on every record.
_FLUSH_WARN_INTERVAL = 60.0

# Retention window (seconds): a shard not written for over an hour is a
# prune CANDIDATE — but only new-format, same-host shards whose owning pid
# is confirmed dead are ever deleted (issue #936 AC5).
_SHARD_STALE_SECONDS = 3600.0

# Magnitude ceiling for a single queue-list sample (a wait in seconds or a
# queue depth): anything above 1e9 is not a real measurement, and letting
# one through lets the cross-shard SUM overflow into OverflowError (which
# would 500 the unauthenticated /metrics scrape — the exact contract
# violation this validation exists to prevent).
_MAX_QUEUE_SAMPLE = 1e9


def _escape_label(value: str) -> str:
    """Escape a label value for the Prometheus text exposition format.

    Issue #936 AC4: every label interpolated into ``metrics_text`` goes
    through here, so shard-sourced strings can never inject ``"``, ``\\``
    or a newline into the exposition.
    """
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


def _host_tag() -> str:
    """Stable 8-hex tag for this host, embedded in new-format shard names.

    Issue #936 AC5: a shard name carries its owner's host so the retention
    prune never deletes a shard whose pid it cannot meaningfully probe (a
    live worker on another host would look dead to this host's pid check).
    """
    return hashlib.sha256(
        socket.gethostname().encode("utf-8", "replace")
    ).hexdigest()[:8]


_HOST_TAG = _host_tag()


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness probe for a LOCAL pid. Conservative on error.

    Issue #936 AC5. NEVER ``os.kill`` on Windows: any signal other than the
    console-event constants is delivered via TerminateProcess. Error
    mapping (both platforms): permission denied / unknown error -> ALIVE
    (a retention leak is strictly safer than deleting a live worker's
    shard); a confirmed nonexistent pid -> dead.
    """
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD,  # dwDesiredAccess
                wintypes.BOOL,  # bInheritHandle
                wintypes.DWORD,  # dwProcessId
            ]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            # 0x1000 = PROCESS_QUERY_LIMITED_INFORMATION.
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                try:
                    return True
                finally:
                    kernel32.CloseHandle(handle)
            err = ctypes.get_last_error()
            # 87 (ERROR_INVALID_PARAMETER): the pid does not exist.
            if err == 87:
                return False
            # 5 (ERROR_ACCESS_DENIED) and anything else: treat as alive —
            # the probe could not establish death.
            return True
        except Exception:  # noqa: BLE001 — probe must never raise
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        # PermissionError and any other probe failure: alive (conservative).
        return True


def _coerce_counter(value) -> "int | None":
    """Counter-field coercion for shard validation: int >= 0, bool rejected.

    Returns None when the value is not a valid counter (issue #936 AC3 —
    the read side validates shape before merging anything).
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _valid_sample(value) -> bool:
    """Queue-list element check: finite int|float in [0, _MAX_QUEUE_SAMPLE],
    bool rejected."""
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)) or value < 0:
        return False
    # Magnitude ceiling BEFORE any float conversion: int-vs-float comparison
    # is exact in Python, so a 10**308 sample (which converts to a finite
    # float fine and would pass isfinite) is rejected here — otherwise two
    # such samples in the merged list overflow sum(waits) into
    # OverflowError at the average, re-opening the /metrics 500 the
    # contract forbids (final-critic delta finding 1, executed). A wait in
    # seconds or a queue depth above 1e9 is not a real measurement.
    if value > _MAX_QUEUE_SAMPLE:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        # json.loads parses exact arbitrary-precision ints; a 400-digit
        # sample is not representable as a finite float and is exactly what
        # the finite-number contract rejects (swarm review L2R-2, executed).
        return False


def _valid_shard(obj) -> "tuple[bool, str]":
    """Validate a parsed shard blob against the shard schema.

    Issue #936 AC3/AC4. Every field is OPTIONAL (older shards stay
    readable); a present field must match its type exactly or the whole
    shard is skipped with a WARNING: bools are not counters, evaluator
    reason keys must be write-side allowlisted, queue samples must be
    finite non-negative numbers. Returns (ok, reason).
    """
    if not isinstance(obj, dict):
        return False, "shard is not a JSON object"
    for key in ("chat_turns", "embedding_cache_hits", "shard_flush_failures"):
        if key in obj and _coerce_counter(obj[key]) is None:
            return False, f"{key} is not a non-negative int"
    reasons = obj.get("evaluator_fail_opens", {})
    if not isinstance(reasons, dict):
        return False, "evaluator_fail_opens is not an object"
    for reason, count in reasons.items():
        if not isinstance(reason, str) or reason not in EVALUATOR_FAIL_OPEN_REASONS:
            return False, f"evaluator_fail_opens key not allowlisted: {reason!r}"
        if _coerce_counter(count) is None:
            return False, "evaluator_fail_opens value is not a non-negative int"
    for list_key in ("queue_waits", "queue_depths"):
        mapping = obj.get(list_key, {})
        if not isinstance(mapping, dict):
            return False, f"{list_key} is not an object"
        depths = list_key == "queue_depths"
        for cls, samples in mapping.items():
            if not isinstance(cls, str) or not isinstance(samples, list):
                return False, f"{list_key}[{cls!r}] is not a list"
            for sample in samples:
                # Depths are int-only (the writer emits int(depth); the
                # approved plan types them int) — a fractional depth is a
                # malformed sample (swarm review L1-8).
                if depths and (isinstance(sample, bool)
                               or not isinstance(sample, int)):
                    return False, f"{list_key}[{cls!r}] has an invalid sample"
                if not _valid_sample(sample):
                    return False, f"{list_key}[{cls!r}] has an invalid sample"
    calls = obj.get("provider_calls", {})
    if not isinstance(calls, dict):
        return False, "provider_calls is not an object"
    for provider, outcomes in calls.items():
        if not isinstance(provider, str) or not isinstance(outcomes, dict):
            return False, f"provider_calls[{provider!r}] is not an object"
        for outcome, count in outcomes.items():
            if _coerce_counter(count) is None:
                return False, f"provider_calls[{provider!r}] value is invalid"
    return True, ""

# Valid reasons for record_evaluator_fail_open (issue #712, AC7 wiring).
EVALUATOR_FAIL_OPEN_REASONS = ("exception", "empty_response", "unexpected_response")

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
                  "http_error", "invalid_json", "admission_rejected",
                  "url_blocked", "invalid_response", "inband_error"):
        # url_blocked/invalid_response added by the issue #717 review: an
        # SSRF-blocked or unparseable provider response is an outage-class
        # outcome, not a soft partial. inband_error (issue #719 review PRR-003)
        # is a mid-stream provider failure the LLM client charges the breaker
        # for; counting it as partial would under-report provider health while
        # the breaker simultaneously treats it as an outage.
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
        self._stage_totals: Dict[str, float] = {}
        self._embedding_cache_hits = 0
        self._evaluator_fail_opens: Dict[str, int] = {}
        # Issue #936 AC1: operator-visible flush-failure accounting plus the
        # rate-limit state for the WARNING (None = the next failure ALWAYS
        # warns; a fresh process whose monotonic clock is below the interval
        # must not have its first failure silenced).
        self._shard_flush_failures = 0
        self._last_flush_warning: Optional[float] = None
        # Per-key rate-limit timestamps for skipped-shard/warning suppression.
        self._warn_state: Dict[str, float] = {}
        # Issue #936 AC5: the host tag makes shard ownership explicit so the
        # retention prune never pid-probes a shard it cannot meaningfully
        # judge (a live worker on another host looks dead to this host).
        self._shard_name = (
            f"telemetry-{_HOST_TAG}-{os.getpid()}-{secrets.token_hex(4)}.json"
        )
        self._otel_observers: list = []
        # Guards every counter/dict mutation and the shard serialization so a
        # recorder can never interleave with another thread's serializer
        # iteration (RuntimeError: dictionary changed size during iteration)
        # or lose += updates. Every recorder call site is on the event loop
        # today, so the lock is currently uncontended; it makes the invariant
        # hold for any future worker-thread caller. Reentrant: _flush_shard /
        # metrics_text / the OTel observer re-enter guarded paths.
        self._lock = threading.RLock()

    # ----------------------------------------------------------- recorders

    def record_chat_turn(self, turn_id: str) -> None:
        if not self.enabled or not turn_id:
            return
        with self._lock:
            self._chat_turns += 1
            # Write-through: the shard is the cross-process aggregation surface,
            # so counters land on disk as they are recorded (not only on scrape).
            self._flush_shard()
        # Observers are foreign code — never inside the critical section.
        self._notify_otel_observers()

    def record_stage(self, turn_id: str, stage: str, duration_seconds: float) -> None:
        if not self.enabled or not turn_id:
            return
        with self._lock:
            self._stage_durations.setdefault(turn_id, {})[stage] = float(
                duration_seconds
            )
            # PR #595 review F-002: keep a running per-stage accumulator so the
            # OTel observer can read totals in O(1) — the previous
            # re-scan-all-history implementation blocked the event loop with
            # O(every-turn-ever) work on every record.
            self._stage_totals[stage] = (
                self._stage_totals.get(stage, 0.0) + float(duration_seconds)
            )
        # Observers are foreign code — never inside the critical section.
        self._notify_otel_observers()

    def record_queue_wait(self, admission_class: str, wait_seconds: float,
                          depth: int = 0) -> None:
        if not self.enabled:
            return
        with self._lock:
            waits = self._queue_waits.setdefault(str(admission_class), [])
            waits.append(float(wait_seconds))
            depths = self._queue_depths.setdefault(str(admission_class), [])
            depths.append(int(depth))
            # Issue #936 AC6: cap BOTH histories at QUEUE_HISTORY_CAP so the
            # write-through flush blob and the lock-hold time stop growing
            # with process lifetime (the oldest samples fall off).
            if len(waits) > QUEUE_HISTORY_CAP:
                del waits[0]
            if len(depths) > QUEUE_HISTORY_CAP:
                del depths[0]

    def record_first_useful_content(self, turn_id: str, seconds: float) -> None:
        if not self.enabled or not turn_id:
            return
        with self._lock:
            self._first_useful[turn_id] = float(seconds)

    def record_provider_call(self, provider: str, outcome: str) -> None:
        if outcome not in PROVIDER_OUTCOMES:
            raise ValueError(
                f"unknown provider outcome {outcome!r}; expected one of "
                f"{PROVIDER_OUTCOMES}"
            )
        if not self.enabled:
            return
        with self._lock:
            per_provider = self._provider_calls.setdefault(str(provider), {})
            per_provider[outcome] = per_provider.get(outcome, 0) + 1
            self._flush_shard()

    def record_embedding_cache(self, hit: bool) -> None:
        if not self.enabled:
            return
        with self._lock:
            if hit:
                self._embedding_cache_hits += 1
            self._flush_shard()

    def record_evaluator_fail_open(self, reason: str) -> None:
        """Count one RetrievalEvaluator fail-open by cause (issue #712).

        The counter is exposed at ``GET /metrics`` as
        ``ragapp_retrieval_evaluator_fail_open_total{reason=...}`` — the
        production read site for the evaluator-outage signal the class
        attribute ``RetrievalEvaluator.fail_open_count`` documents but
        nothing used to read (audit TQ-instruction-batch-5-02). Write-through
        like every counter so cross-process scrapers see it immediately.
        """
        if not self.enabled:
            return
        if reason not in EVALUATOR_FAIL_OPEN_REASONS:
            raise ValueError(
                f"unknown evaluator fail-open reason {reason!r}; expected one "
                f"of {EVALUATOR_FAIL_OPEN_REASONS}"
            )
        with self._lock:
            self._evaluator_fail_opens[reason] = (
                self._evaluator_fail_opens.get(reason, 0) + 1
            )
            self._flush_shard()

    # ------------------------------------------------------------- reading

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "chat_turns": self._chat_turns,
                "stage_durations": {
                    turn: dict(stages)
                    for turn, stages in self._stage_durations.items()
                },
                "queue_waits": {
                    cls: list(v) for cls, v in self._queue_waits.items()
                },
                "queue_depths": {
                    cls: list(v) for cls, v in self._queue_depths.items()
                },
                "first_useful_content": dict(self._first_useful),
                "provider_calls": {
                    provider: dict(outcomes)
                    for provider, outcomes in self._provider_calls.items()
                },
                "embedding_cache_hits": self._embedding_cache_hits,
                "evaluator_fail_opens": dict(self._evaluator_fail_opens),
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

    def stage_totals(self) -> Dict[str, float]:
        """Running per-stage duration totals (O(1); PR #595 review F-002)."""
        with self._lock:
            return dict(self._stage_totals)

    def reset(self) -> None:
        with self._lock:
            self._chat_turns = 0
            self._stage_durations = {}
            self._stage_totals = {}
            self._queue_waits = {}
            self._queue_depths = {}
            self._first_useful = {}
            self._provider_calls = {}
            self._embedding_cache_hits = 0
            self._evaluator_fail_opens = {}
            self._shard_flush_failures = 0
            self._last_flush_warning = None
            self._warn_state = {}
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
        # Serialize the LIVE dicts under the lock: without it, a recorder on
        # another thread mutating self._queue_waits et al. during json.dumps
        # raises RuntimeError (dictionary changed size during iteration)
        # straight out of the recorder call (issue #712, P01-SK2-12). The
        # guard is Exception, not OSError, for exactly that reason.
        with self._lock:
            try:
                self.registry_dir.mkdir(parents=True, exist_ok=True)
                counts = {
                    "chat_turns": self._chat_turns,
                    "queue_waits": self._queue_waits,
                    "queue_depths": self._queue_depths,
                    "provider_calls": self._provider_calls,
                    "embedding_cache_hits": self._embedding_cache_hits,
                    "evaluator_fail_opens": self._evaluator_fail_opens,
                    "shard_flush_failures": self._shard_flush_failures,
                }
                # Issue #936 AC2: write to a temp sibling then os.replace so a
                # crash mid-write (or a scrape racing the write) never
                # observes a torn shard — the previous flush stays intact
                # and the swap is atomic. The ".tmp" suffix never matches the
                # readers'/prune's "telemetry-*.json" glob.
                tmp_path = self.registry_dir / (self._shard_name + ".tmp")
                tmp_path.write_text(_dumps(counts), encoding="utf-8")
                os.replace(tmp_path, self.registry_dir / self._shard_name)
            except Exception:  # noqa: BLE001 — telemetry must never raise
                # Issue #936 AC1: a persistent flush failure must be
                # operator-visible — count it ALWAYS, warn at most once per
                # _FLUSH_WARN_INTERVAL (None state means the first failure
                # warns unconditionally). The DEBUG record keeps the full
                # traceback.
                self._shard_flush_failures += 1
                now = time.monotonic()
                if (
                    self._last_flush_warning is None
                    or now - self._last_flush_warning >= _FLUSH_WARN_INTERVAL
                ):
                    self._last_flush_warning = now
                    logger.warning(
                        "Telemetry shard flush failed (shard=%s); counters "
                        "are not persisting to the registry directory — "
                        "see ragapp_telemetry_shard_flush_failures_total",
                        self._shard_name,
                    )
                logger.debug("telemetry shard flush failed", exc_info=True)

    def _rate_limited_warning(self, key: str, message: str, *args) -> None:
        """Log one WARNING per key per _FLUSH_WARN_INTERVAL (issue #936 AC3).

        Skipped/unreadable shards warn once a minute per shard instead of on
        every scrape; the underlying condition stays observable without
        flooding the log. The _warn_state map is guarded by the reentrant
        lock (implementation-review F3: callers may already hold it; every
        counter/dict mutation on this class goes through the same guard).
        """
        with self._lock:
            now = time.monotonic()
            last = self._warn_state.get(key)
            if last is not None and now - last < _FLUSH_WARN_INTERVAL:
                return
            self._warn_state[key] = now
        logger.warning(message, *args)

    def _aggregate_counts(self) -> dict:
        totals = {
            "chat_turns": 0,
            "queue_waits": {},
            "queue_depths": {},
            "provider_calls": {},
            "embedding_cache_hits": 0,
            "evaluator_fail_opens": {},
            "shard_flush_failures": 0,
        }
        shards: list = []
        own_disk_failures = 0
        if self.registry_dir is not None and self.registry_dir.exists():
            now = time.time()
            for entry in sorted(self.registry_dir.glob("telemetry-*.json")):
                # Retention (issue #936 AC5, swarm review PRR-018 before it):
                # a shard stale for over an hour is a prune CANDIDATE, but it
                # is deleted ONLY when ownership is established as local
                # (new-format name carrying THIS host's tag) AND the pid is
                # confirmed dead. Foreign-host shards (a cross-host pid probe
                # is meaningless — a live remote worker would look dead
                # here), legacy-format shards, and unparsable names are all
                # RETAINED: unknown ownership is never conflated with
                # confirmed death. Retention HONESTY (final-critic finding
                # 3): each retained shard's SIZE is bounded (its histories
                # are capped), but the shard COUNT is not — under
                # per-incarnation hostnames (Docker container ids, K8s pod
                # names) every past incarnation's shard is retained forever
                # until an operator removes it, because this host cannot
                # safely judge those pids. A host-independent liveness
                # signal (or a long foreign-stale window) is the
                # maintainer-level follow-up. ASSUMPTION (swarm review
                # L2R-3): the host tag treats a hostname as a unique host
                # identity — two simultaneously-live hosts sharing one
                # hostname over a shared registry dir (cloned VMs,
                # StatefulSet pod-name reuse) can mis-judge each other's
                # pids; the victim's next write-through flush recreates its
                # shard, so the loss is transient.
                try:
                    if entry.name != self._shard_name and (
                        now - entry.stat().st_mtime > _SHARD_STALE_SECONDS
                    ):
                        stem = entry.name[: -len(".json")]
                        parts = stem.split("-")
                        if (
                            len(parts) == 4
                            and parts[0] == "telemetry"
                            and parts[1] == _HOST_TAG
                            and parts[2].isascii()
                            and parts[2].isdigit()
                            and not _pid_alive(int(parts[2]))
                        ):
                            entry.unlink(missing_ok=True)
                            continue
                except OSError:  # pragma: no cover — racing unlink/stat
                    continue
                # Issue #936 AC3: unreadable shards (OSError) and malformed
                # or hostile payloads (shape/allowlist violations) are
                # skipped with a rate-limited WARNING — never a 500 on
                # /metrics and never merged into the exposition.
                # UnicodeDecodeError (a ValueError subclass — binary-corrupt
                # or non-UTF-8 bytes) is caught HERE at the read leg, not by
                # the json.loads ValueError arm below (implementation-review
                # F1: base caught (OSError, ValueError) around both).
                try:
                    raw = entry.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    self._rate_limited_warning(
                        f"read:{entry.name}",
                        "Telemetry shard unreadable (path=%s); skipping it "
                        "in /metrics aggregation",
                        entry,
                    )
                    continue
                try:
                    parsed = json.loads(raw)
                # RecursionError (RuntimeError subclass) rides the same leg:
                # deeply nested hostile JSON blew the 500 contract before
                # this widening (swarm review L2R-1, executed at HEAD+base).
                except (ValueError, RecursionError):
                    self._rate_limited_warning(
                        f"parse:{entry.name}",
                        "Telemetry shard is not valid JSON (path=%s); "
                        "skipping it in /metrics aggregation",
                        entry,
                    )
                    continue
                valid, reason = _valid_shard(parsed)
                if not valid:
                    self._rate_limited_warning(
                        f"shape:{entry.name}",
                        "Telemetry shard failed validation (path=%s, %s); "
                        "skipping it in /metrics aggregation",
                        entry,
                        reason,
                    )
                    continue
                if entry.name == self._shard_name:
                    own_disk_failures = int(
                        parsed.get("shard_flush_failures", 0)
                    )
                shards.append(parsed)
            # Crash-leftover temp siblings (issue #936 AC2): an interrupted
            # atomic write strands a ".tmp" file; reclaim stale ones. An
            # in-flight temp write has a fresh mtime and is naturally spared.
            for entry in sorted(
                self.registry_dir.glob("telemetry-*.json.tmp")
            ):
                try:
                    if now - entry.stat().st_mtime > _SHARD_STALE_SECONDS:
                        entry.unlink(missing_ok=True)
                except OSError:  # pragma: no cover — racing unlink/stat
                    continue
        else:
            shards.append(self._local_counts())
            # The fallback "shard" IS the local counts dict: its
            # shard_flush_failures value equals the in-memory counter, so
            # subtracting it back out keeps the replace-with-local rule
            # exact in this branch too.
            own_disk_failures = self._shard_flush_failures
        # Merge under the lock: the local shard references LIVE dicts, so an
        # unlocked .items() iteration races concurrent recorders (the same
        # RuntimeError class _flush_shard guards against).
        with self._lock:
            for shard in shards:
                totals["chat_turns"] += int(shard.get("chat_turns", 0))
                totals["embedding_cache_hits"] += int(
                    shard.get("embedding_cache_hits", 0)
                )
                for reason, count in shard.get("evaluator_fail_opens", {}).items():
                    merged_eval = totals["evaluator_fail_opens"]
                    merged_eval[reason] = merged_eval.get(reason, 0) + int(count)
                for cls, waits in shard.get("queue_waits", {}).items():
                    # Per-SHARD cap only (final-critic finding 1): the cap
                    # bounds what each process retains and serializes; the
                    # CROSS-SHARD aggregate sums every shard's retained
                    # window. Trimming the merged list instead would let
                    # whichever shard sorts last monopolize the queue-wait
                    # average (executed probes: two capped shards, true mean
                    # 5.0, the merged trim reported 0.0 or 10.0 —
                    # whichever shard sorts last wins).
                    merged_waits = totals["queue_waits"].setdefault(cls, [])
                    merged_waits.extend(waits[-QUEUE_HISTORY_CAP:])
                for cls, depths in shard.get("queue_depths", {}).items():
                    merged_depths = totals["queue_depths"].setdefault(cls, [])
                    merged_depths.extend(depths[-QUEUE_HISTORY_CAP:])
                for provider, outcomes in shard.get("provider_calls", {}).items():
                    merged = totals["provider_calls"].setdefault(provider, {})
                    for outcome, count in outcomes.items():
                        merged[outcome] = merged.get(outcome, 0) + int(count)
            # Flush-failure accounting (issue #936 AC1): every shard's
            # persisted counter sums, EXCEPT this process's own disk copy,
            # whose contribution is REPLACED by the live in-memory count —
            # a failing writer cannot persist its own counter, and a
            # transient failure followed by a successful flush must read 1,
            # not 2. In the registry-absent fallback the only "shard" is the
            # local counts dict (own_disk_failures == the in-memory number),
            # so the same arithmetic is correct there too.
            persisted_total = 0
            for shard in shards:
                persisted_total += int(shard.get("shard_flush_failures", 0))
            totals["shard_flush_failures"] = (
                persisted_total - own_disk_failures + self._shard_flush_failures
            )
        return totals

    def _local_counts(self) -> dict:
        return {
            "chat_turns": self._chat_turns,
            "queue_waits": self._queue_waits,
            "queue_depths": self._queue_depths,
            "provider_calls": self._provider_calls,
            "embedding_cache_hits": self._embedding_cache_hits,
            "evaluator_fail_opens": self._evaluator_fail_opens,
            "shard_flush_failures": self._shard_flush_failures,
        }

    def metrics_text(self) -> str:
        if not self.enabled:
            return ""
        self._flush_shard()
        totals = self._aggregate_counts()
        # Issue #936 AC7: every HELP/TYPE line and every sample interpolates
        # the METRIC_* constants — no metric name appears as a string literal
        # here, so a constant rename can never desync the exposition from
        # its samples (label values go through _escape_label per AC4).
        lines = [
            f"# HELP {METRIC_CHAT_TURNS} Chat turns recorded.",
            f"# TYPE {METRIC_CHAT_TURNS} counter",
            f"{METRIC_CHAT_TURNS} {totals['chat_turns']}",
        ]
        for cls in sorted(totals["queue_depths"]):
            depths = totals["queue_depths"][cls]
            lines.append(
                f'{METRIC_QUEUE_DEPTH}{{admission_class="{_escape_label(cls)}"}} '
                f"{depths[-1] if depths else 0}"
            )
        for cls in sorted(totals["queue_waits"]):
            waits = totals["queue_waits"][cls]
            avg = (sum(waits) / len(waits)) if waits else 0.0
            lines.append(
                f'{METRIC_QUEUE_WAIT}{{admission_class="{_escape_label(cls)}"}} '
                f"{avg:.6f}"
            )
        for provider in sorted(totals["provider_calls"]):
            for outcome in sorted(totals["provider_calls"][provider]):
                count = totals["provider_calls"][provider][outcome]
                lines.append(
                    f'{METRIC_PROVIDER_CALLS}{{provider="{_escape_label(provider)}",'
                    f'outcome="{_escape_label(outcome)}"}} {count}'
                )
        lines.append(
            f"# HELP {METRIC_EMBEDDING_CACHE_HITS} Embedding cache hits."
        )
        lines.append(f"# TYPE {METRIC_EMBEDDING_CACHE_HITS} counter")
        lines.append(
            f"{METRIC_EMBEDDING_CACHE_HITS} {totals['embedding_cache_hits']}"
        )

        lines.append(
            f"# HELP {METRIC_EVALUATOR_FAIL_OPEN} Retrieval "
            "evaluator fail-opens by cause (issue #712)."
        )
        lines.append(f"# TYPE {METRIC_EVALUATOR_FAIL_OPEN} counter")
        for reason in sorted(totals["evaluator_fail_opens"]):
            count = totals["evaluator_fail_opens"][reason]
            lines.append(
                f'{METRIC_EVALUATOR_FAIL_OPEN}{{reason="{_escape_label(reason)}"}} '
                f"{count}"
            )
        lines.append(
            f"# HELP {METRIC_SHARD_FLUSH_FAILURES} Failed telemetry shard "
            "flushes (counters not persisting to the registry directory)."
        )
        lines.append(f"# TYPE {METRIC_SHARD_FLUSH_FAILURES} counter")
        lines.append(
            f"{METRIC_SHARD_FLUSH_FAILURES} {totals['shard_flush_failures']}"
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
    global _tracer, _tracer_resolved
    _tracer = None
    _tracer_resolved = False
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



# ------------------------------------------------------------------ tracing
# Issue #518 frontier-audit amendment: "Instrument FastAPI and httpx with
# OpenTelemetry ... emit gen_ai.* spans". Spans are emitted through the SAME
# optional extra as the OTLP metric bridge (backend/requirements-otel.txt):
# when the extra is absent (the default, air-gapped install) the tracer is a
# no-op shim and nothing here imports opentelemetry at all.


class _NoOpSpan:
    """Context-manager stand-in returned by the no-op tracer."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def set_attribute(self, key, value) -> None:
        return None


class _NoOpTracer:
    """Zero-cost tracer used when the optional OTel extra is not installed."""

    def start_as_current_span(self, name: str, **kwargs):
        return _NoOpSpan()

    def start_span(self, name: str, **kwargs):
        return _NoOpSpan()


_tracer = None
_tracer_resolved = False


def _resolve_tracer():
    """Resolve the span tracer, installing a real pipeline when possible.

    With the optional extra importable and telemetry enabled, this installs
    OUR OWN TracerProvider (PR #595 review F-001a: relying on the global
    default left every span non-recording — nothing was ever exported, and
    the ProxyTracer defeated the no-op gate so the middleware attached for
    zero benefit). With an OTLP endpoint configured the provider exports
    spans via BatchSpanProcessor; without one, spans are still REAL
    (recording, parented, measured) and simply dropped at the end — the
    documented export upgrade path is setting the endpoint. Without the
    extra (the default, air-gapped install) this returns the no-op shim and
    nothing opentelemetry-related is imported.
    """
    global _tracer, _tracer_resolved
    if _tracer_resolved:
        return _tracer
    # Resolve telemetry FIRST: init_telemetry() resets the tracer globals
    # (F-009 re-resolve), and it can run inside the get_telemetry() call
    # below — mutating the globals before it would be clobbered.
    telemetry_enabled = get_telemetry().enabled
    _tracer_resolved = True
    _tracer = _NoOpTracer()
    if telemetry_enabled:
        try:
            from opentelemetry import trace as _otel_trace
            from opentelemetry.sdk.trace import TracerProvider as _SdkProvider

            sdk_provider = _SdkProvider()
            endpoint = os.environ.get(_OTLP_EXPORT_ENV, "").strip()
            if endpoint:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )
                from opentelemetry.sdk.trace.export import BatchSpanProcessor

                sdk_provider.add_span_processor(
                    BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
                )
            _otel_trace.set_tracer_provider(sdk_provider)
            _tracer = sdk_provider.get_tracer("ragapp.telemetry")
        except ImportError:
            # Documented off-state: the optional extra is not installed.
            pass
        except Exception:  # noqa: BLE001 — tracing must never break records
            logger.debug("tracer resolution failed", exc_info=True)
    return _tracer


def get_tracer():
    """Public tracer surface (span emission; no-op without the extra)."""
    return _resolve_tracer()


def _active_span_context():
    """Return (trace_id_hex, span_id_hex) from the current span, or None.

    Used by correlation_headers() so the outbound W3C traceparent carries a
    REAL span context when tracing is active, instead of a synthesized id.
    """
    tracer = _resolve_tracer()
    if isinstance(tracer, _NoOpTracer):
        return None
    try:
        from opentelemetry import trace as _otel_trace

        ctx = _otel_trace.get_current_span().get_span_context()
        if ctx and ctx.is_valid:
            return f"{ctx.trace_id:032x}", f"{ctx.span_id:016x}"
    except Exception:  # noqa: BLE001 — correlation must never raise
        logger.debug("span-context read failed", exc_info=True)
    return None


def start_span(name: str, **kwargs):
    """Start a span (no-op context manager without the optional extra)."""
    return _resolve_tracer().start_as_current_span(name, **kwargs)


def register_span_middleware(app) -> None:
    """Attach the per-request server-span middleware when tracing is live.

    No-op (registers nothing) when the tracer is the shim, so default
    installs keep the exact request path they had before.
    """
    if isinstance(_resolve_tracer(), _NoOpTracer):
        return
    from app.middleware.telemetry_span import TelemetrySpanMiddleware

    app.add_middleware(TelemetrySpanMiddleware)


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
    # W3C traceparent from the ACTIVE span context when tracing is live
    # (issue #518 amendment); fall back to the deterministic synthesized ids
    # so the outbound header is stable for a given turn either way.
    span_ctx = _active_span_context()
    if span_ctx is not None:
        trace_id, span_id = span_ctx
    else:
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
    # E3 closure (#518 amendment, "emit gen_ai.* spans and metrics"): a
    # gen_ai-named operation-duration counter driven by the stage
    # recorder, so the OTLP bridge exports GenAI-convention metrics
    # alongside the ragapp_* bridge. Naming follows the Development-status
    # semantic-conventions-genai set — do not describe as stable. The
    # operation names here are this app's stage names (planning /
    # searching / reading / generation), not GenAI client operations.
    genai_counter = meter.create_counter("gen_ai.client.operation.duration")
    # Publish the aggregate on each export interval by reading the same
    # snapshot /metrics serves — the shard aggregate, so replicas sum.
    observed = {"last": 0, "stages": {}}

    def _observe(_options):
        # Fired on each chat-turn/stage record; the OTel reader exports the
        # accumulated counters on its own cadence. O(1) per record: reads
        # the running accumulator, never the per-turn history (F-002).
        totals = telemetry._aggregate_counts()
        delta = totals["chat_turns"] - observed["last"]
        observed["last"] = totals["chat_turns"]
        if delta > 0:
            counter.add(delta)
        stage_totals = telemetry.stage_totals()
        for stage, total in stage_totals.items():
            stage_delta = total - observed["stages"].get(stage, 0.0)
            if stage_delta > 0:
                genai_counter.add(
                    stage_delta,
                    attributes={"gen_ai.operation.name": stage},
                )
        observed["stages"] = stage_totals

    otel_metrics.set_meter_provider(provider)
    telemetry.attach_otel_observer(_observe)
    _otel_export_started = True
    logger.info("OTLP metric export started (endpoint configured)")
    return True
