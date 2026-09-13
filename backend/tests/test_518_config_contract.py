"""Issue #518 acceptance check C8 / AC8 — config/env contract + local ruff
gate for the new admission/telemetry settings.

Every NEW settings key referenced by the C2 (admission) and C3 (telemetry)
test modules must:
  1. exist as a field on ``app.config.Settings`` (so production wiring can
     read it), and
  2. appear in ``.env.example`` (active or commented form), keeping the
     config-env contract in sync.

Also runs ``ruff check`` over ``backend`` in a subprocess, mirroring the CI
lint gate (skipped with a visible sentinel note when ruff is unavailable
locally; CI enforces it regardless).

Base tree fails on the missing-new-keys portion with a clear sentinel.
"""

import re
import subprocess
import sys
from pathlib import Path

from app.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]

# Frozen new-key set — names MUST match what test_518_admission.py and
# test_518_telemetry.py import / construct with.
NEW_SETTINGS_KEYS = [
    "admission_enabled",
    "admission_chat_budget",
    "admission_instant_budget",
    "admission_embedding_budget",
    "admission_reranking_budget",
    "admission_vision_budget",
    "admission_background_budget",
    "admission_queue_max_size",
    "admission_deadline_seconds",
    "admission_store_url",
    "telemetry_enabled",
]


def test_new_settings_keys_exist_on_settings():
    fields = getattr(Settings, "model_fields", None) or {}
    for key in NEW_SETTINGS_KEYS:
        assert key in fields, (
            f"518-C8 MISSING SETTINGS KEY: {key} is not defined on "
            "app.config.Settings — the admission/telemetry modules cannot "
            "be configured (config-env contract out of sync)"
        )


def test_new_settings_keys_documented_in_env_example():
    env_text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for key in NEW_SETTINGS_KEYS:
        # Active or commented form both document the key.
        assert re.search(rf"^(?:#\s*)?{re.escape(key)}=", env_text, re.MULTILINE), (
            f"518-C8 ENV EXAMPLE MISSING KEY: {key} is not documented in "
            ".env.example (config-env contract requires every Settings "
            "key to appear there)"
        )


def test_ruff_check_backend_passes_locally():
    try:
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "backend"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=300,
        )
    except FileNotFoundError:
        print(
            "518-C8 RUFF SKIPPED LOCALLY: ruff is not installed in this "
            "interpreter; CI enforces the lint gate regardless"
        )
        return
    assert result.returncode == 0, (
        "518-C8 RUFF FAILED: 'ruff check backend' reported findings "
        f"(exit {result.returncode}):\n{result.stdout[-2000:]}"
        f"\n{result.stderr[-500:]}"
    )
