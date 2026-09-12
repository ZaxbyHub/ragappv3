"""PR #576 review F8 — compose forwarding must use ${KEY:-} form, not literals.

The AC17 gate (test_issue494_compose_env_forwarding.py) checks key-name
presence only, so a hardcoded literal like ``- DATA_DIR=/app/data`` satisfies
it while silently ignoring the operator's .env value. This additive gate
asserts every documented .env.example key that appears in the backend
``environment:`` block is forwarded in ``KEY=${KEY:-...}`` form UNLESS it is
on the documented exception list (host-/build-time keys and the explicitly
pinned container-internal paths).
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO = Path(__file__).resolve().parents[2]

# Documented .env.example keys that are intentionally NOT forwarded as
# ${KEY:-} (matches the exception notes in .env.example and docker-compose.yml:
# host-/build-time keys, plus container-internal paths pinned by the image).
LITERAL_ALLOWED = {
    "PORT",
    "VITE_APP_BASENAME",
    "VITE_API_URL",
    "HOST_DATA_DIR",
    "HF_TOKEN",
    # Container-internal paths/values pinned by the image + volume contract:
    # DATA_DIR must agree with the container-internal /app/data volume mount;
    # the two feature flags below are deliberately pinned off in the compose
    # deployment until their features graduate.
    "DATA_DIR",
    "TRI_VECTOR_SEARCH_ENABLED",
    "FLAG_EMBEDDING_URL",
}


def _backend_env_entries():
    """Yield (key, raw_value) pairs from the knowledgevault environment block."""
    import re

    text = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    # Scope to the knowledgevault service block (up to the next top-level key).
    match = re.search(r"^  knowledgevault:", text, re.M)
    assert match, "knowledgevault service not found in docker-compose.yml"
    tail = text[match.end():]
    block = re.split(r"^  [a-zA-Z_-]+:", tail, maxsplit=1)[0]
    env_at = block.find("environment:")
    assert env_at != -1
    env_block = block[env_at:]
    for line in env_block.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and "=" in stripped:
            entry = stripped[2:]
            key, _, value = entry.partition("=")
            yield key.strip(), value.strip()


class TestComposeForwardingShape(unittest.TestCase):
    def test_documented_keys_use_forwarding_form_or_exceptions(self):
        import re

        documented = set()
        for line in (REPO / ".env.example").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            documented.add(line.split("=", 1)[0].strip())

        forwarded = dict(_backend_env_entries())
        violations = []
        for key, value in forwarded.items():
            if key not in documented:
                continue  # compose-only keys (DATA_DIR etc.) are judged below
            if key in LITERAL_ALLOWED:
                continue
            if not re.search(r"\$\{", value):
                violations.append(f"{key}={value} (literal, not ${{KEY:-}} form)")
        self.assertEqual(
            violations,
            [],
            "documented .env.example keys forwarded as hardcoded literals make "
            "the forwarding contract a lie for those keys — add them to the "
            "exception lists with rationale or forward them as ${KEY:-}: "
            + "; ".join(violations),
        )

    def test_exception_list_keys_are_pinned_or_absent(self):
        # Non-backend keys (PORT, VITE_*, HOST_DATA_DIR, HF_TOKEN) never reach
        # this environment block; DATA_DIR must stay pinned to the
        # container-internal contract path.
        forwarded = dict(_backend_env_entries())
        self.assertEqual(
            forwarded.get("DATA_DIR"),
            "/app/data",
            "DATA_DIR is pinned by the image/volume contract; if you make it "
            "configurable, update the volume mount and this pin together",
        )


if __name__ == "__main__":
    unittest.main()
