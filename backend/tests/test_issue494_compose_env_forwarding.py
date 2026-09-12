"""
Issue #494 acceptance check — AC17 (CONFIG-007): docker-compose.yml forwards
only a subset of the documented .env.example variables into the backend
container.

Root cause (verified at base a543361): the backend service's
``environment:`` block lists ~97 entries, but many variables documented as
tunable in .env.example (AUTO_SCAN_*, CHUNK_SIZE*/CHUNK_OVERLAP*,
RETRIEVAL_*, MAX_DISTANCE_THRESHOLD, VECTOR_METRIC, EMBEDDING_BATCH_*,
EMBEDDING_DOC/QUERY_PREFIX, MODEL-tuning knobs, MAINTENANCE_MODE, the whole
IMAP_* family, SEARCH_SEMAPHORE_TIMEOUT_SECONDS, WRITE_LOCK_TIMEOUT_SECONDS,
ACTIVE_USER_CACHE_TTL_SECONDS, MEMORY_STORE_POOL_SIZE, DB_POOL_MAX_SIZE,
ALLOWED_EXTENSIONS, USERS_ENABLED, JWT_ALGORITHM, ADMIN_RATE_LIMIT,
CSRF_TOKEN_TTL, ALLOWED_HOSTS, ENABLE_MODEL_VALIDATION, HEALTH_CHECK_API_KEY,
...) are not forwarded, so setting them in .env has no effect on the
dockerized backend.

Check (deterministic file parse — no docker binary, no network): every
ACTIVE (uncommented) KEY=... variable documented in .env.example must appear
in the backend service's ``environment:`` list (or the service must declare
``env_file:``), except for an explicit host-only exception list declared and
justified below.

Exception policy (kept minimal):
- host-side / build-time values that are not backend container runtime env;
- the one variable consumed by a sidecar container (already forwarded there).
Secrets that ARE already forwarded at base (ADMIN_SECRET_TOKEN,
JWT_SECRET_KEY) stay in the checked set — they pass mechanically.
"""

import re
import unittest
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE_PATH = _REPO_ROOT / "docker-compose.yml"
_ENV_EXAMPLE_PATH = _REPO_ROOT / ".env.example"

# The backend service is the one built from the repo Dockerfile.
_BACKEND_SERVICE = "knowledgevault"

# Documented .env.example variables that legitimately must NOT be forwarded
# into the backend container's environment. Keep this minimal and justified.
COMPOSE_ENV_EXCEPTIONS = {
    "PORT": "host-side port mapping (compose ports:); the in-container "
            "listener is fixed at 9090 by the Dockerfile CMD",
    "VITE_APP_BASENAME": "frontend build-time argument (Dockerfile build "
                         "stage), not backend runtime env",
    "VITE_API_URL": "frontend build-time argument (Dockerfile build stage), "
                    "not backend runtime env",
    "HOST_DATA_DIR": "host-side path for the data volume mount; must stay on "
                     "the host side of the bind mount",
    "HF_TOKEN": "consumed by the harrier-embed sidecar (already forwarded in "
                "its own environment), not read by the backend",
}

_ENV_KEY_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=")


def _documented_env_names():
    """Active (uncommented) KEY=... lines in .env.example.

    Commented-out lines (e.g. '#REDIS_IO_TIMEOUT_SECONDS=1') document
    optional overrides, not the documented default contract, so they are not
    part of the mechanical set.
    """
    names = []
    for line in _ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ENV_KEY_RE.match(stripped)
        if match:
            names.append(match.group(1))
    return names


def _backend_env_names():
    """Env names forwarded to the backend service (or None when env_file)."""
    compose = yaml.safe_load(_COMPOSE_PATH.read_text(encoding="utf-8"))
    try:
        service = compose["services"][_BACKEND_SERVICE]
    except KeyError as exc:
        raise AssertionError(
            f"backend service {_BACKEND_SERVICE!r} not found in "
            f"docker-compose.yml — update this check if the service was "
            f"renamed"
        ) from exc

    if service.get("env_file"):
        # An env_file directive injects every documented var wholesale.
        return None

    names = set()
    entries = service.get("environment") or []
    if isinstance(entries, dict):
        names.update(entries.keys())
    else:
        for entry in entries:
            if isinstance(entry, str):
                names.add(entry.split("=", 1)[0].strip())
            else:
                names.update(entry.keys())
    return names


class TestComposeForwardsDocumentedEnv(unittest.TestCase):
    """AC17 — DISCRIMINATING: documented .env vars must reach the backend."""

    def test_ac17_documented_env_vars_forwarded_to_backend_service(self):
        self.assertTrue(_COMPOSE_PATH.exists(), "docker-compose.yml not found")
        self.assertTrue(_ENV_EXAMPLE_PATH.exists(), ".env.example not found")

        forwarded = _backend_env_names()
        if forwarded is None:
            self.skipTest("backend service declares env_file — all vars forwarded")

        documented = _documented_env_names()
        self.assertGreater(len(documented), 100,
                           "unexpectedly few documented .env.example vars")

        checked = [v for v in documented if v not in COMPOSE_ENV_EXCEPTIONS]
        missing = sorted(v for v in checked if v not in forwarded)

        print("AC17 CHECK: FAIL")
        self.assertEqual(
            missing,
            [],
            f"{len(missing)} documented .env.example variables are not "
            f"forwarded to the backend service environment in "
            f"docker-compose.yml — tuning them in .env silently has no "
            f"effect on the container. Missing: {missing}",
        )


if __name__ == "__main__":
    unittest.main()
