"""
Regression tests for issue #288 — documentation/config drift.

Each test would have caught the specific drift defect before the fix and
verifies that the documentation/markdown/config values now match the code.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def env_value(env_text: str, name: str) -> str | None:
    match = re.search(rf"^{re.escape(name)}=(.*)$", env_text, re.MULTILINE)
    return match.group(1).strip() if match else None


def compose_default(compose_text: str, name: str) -> str | None:
    match = re.search(rf"\$\{{\s*{re.escape(name)}:-([^}}]*)}}", compose_text)
    return match.group(1).strip() if match else None


# ── A8-6: RERANKER_TOP_N must match across .env.example, docker-compose, config.py ──

class TestRerankerTopNConsistency:
    """A8-6: .env.example RERANKER_TOP_N must match code default (7) and compose default (7)."""

    def test_env_example_reranker_top_n_matches_config(self):
        env_text = read(".env.example")
        env_val = env_value(env_text, "RERANKER_TOP_N")
        assert env_val == "7", (
            f".env.example RERANKER_TOP_N is {env_val!r}, expected '7' to match config.py"
        )

    def test_compose_reranker_top_n_default_matches_config(self):
        compose_text = read("docker-compose.yml")
        compose_val = compose_default(compose_text, "RERANKER_TOP_N")
        assert compose_val == "7", (
            f"docker-compose.yml RERANKER_TOP_N default is {compose_val!r}, expected '7'"
        )


# ── A8-7: README rate-limit table must match config.py defaults ──

class TestReadmeRateLimitTable:
    """A8-7: README.md rate-limit table values must match config.py defaults (30/30)."""

    def test_readme_search_rate_limit_is_30(self):
        readme = read("README.md")
        # The table row format is: | `SEARCH_RATE_LIMIT` | `30` | ...
        match = re.search(r"\|\s*`SEARCH_RATE_LIMIT`\s*\|\s*`(\d+)`", readme)
        assert match, "SEARCH_RATE_LIMIT row not found in README.md"
        assert match.group(1) == "30", (
            f"README.md SEARCH_RATE_LIMIT shows {match.group(1)}, expected 30"
        )

    def test_readme_vault_create_rate_limit_is_30(self):
        readme = read("README.md")
        match = re.search(r"\|\s*`VAULT_CREATE_RATE_LIMIT`\s*\|\s*`(\d+)`", readme)
        assert match, "VAULT_CREATE_RATE_LIMIT row not found in README.md"
        assert match.group(1) == "30", (
            f"README.md VAULT_CREATE_RATE_LIMIT shows {match.group(1)}, expected 30"
        )


# ── A8-4: OPTIMIZE_MODE must be consistent across .env.example, docker-compose, config.py ──

class TestOptimizeModeConsistency:
    """A8-4: OPTIMIZE_MODE default should match config.py ('periodic') everywhere."""

    def test_env_example_optimize_mode_matches_config(self):
        env_text = read(".env.example")
        val = env_value(env_text, "OPTIMIZE_MODE")
        assert val == "periodic", (
            f".env.example OPTIMIZE_MODE is {val!r}, expected 'periodic' to match config.py"
        )

    def test_compose_optimize_mode_default_matches_config(self):
        compose_text = read("docker-compose.yml")
        val = compose_default(compose_text, "OPTIMIZE_MODE")
        assert val == "periodic", (
            f"docker-compose.yml OPTIMIZE_MODE default is {val!r}, expected 'periodic'"
        )


# ── A8-2: MAINTENANCE_MODE comment must not claim it blocks uploads ──

class TestMaintenanceModeComment:
    """A8-2: .env.example must not falsely claim MAINTENANCE_MODE blocks uploads."""

    def test_env_example_does_not_claim_upload_blocking(self):
        env_text = read(".env.example")
        # Find the MAINTENANCE_MODE block comment
        match = re.search(r"#\s*(.*?\n)*?#.*MAINTENANCE_MODE", env_text)
        # Check that 'blocks' + 'upload' don't appear in the comment lines
        # above MAINTENANCE_MODE
        block_start = env_text.find("MAINTENANCE MODE")
        if block_start == -1:
            pytest.skip("MAINTENANCE MODE section not found")
        block = env_text[block_start : env_text.find("MAINTENANCE_MODE=", block_start)]
        assert "blocks" not in block.lower(), (
            "MAINTENANCE_MODE comment still claims it blocks something"
        )
        assert "does NOT block" in block, (
            "MAINTENANCE_MODE comment should clarify it does NOT block uploads"
        )


# ── A8-3: admin-guide.md must not claim files default to vault 1 ──

class TestVaultMigrationDoc:
    """A8-3: admin-guide.md must not claim unmigratable files default to vault 1."""

    def test_admin_guide_no_orphan_vault_claim(self):
        admin_guide = read("docs/admin-guide.md")
        assert "orphan vault" not in admin_guide.lower(), (
            "admin-guide.md still references 'orphan vault' — contradicts code"
        )
        # The specific drift was the line "it defaults to the orphan vault (vault 1)"
        # which was changed to "logs a warning and skips the file". Check that
        # the migration note does not claim files are assigned to vault 1.
        note_match = re.search(
            r"cannot be associated.*?vault.*?(?:\n|$)",
            admin_guide,
        )
        if note_match:
            note = note_match.group(0).lower()
            assert "vault 1" not in note or "skip" in note, (
                "admin-guide.md migration note should say files are skipped, not assigned to vault 1"
            )


# ── DD-1: admin-guide.md must not claim invite-via-email ──

class TestInviteEmailClaim:
    """DD-1: admin-guide.md must not claim invite users 'via email'."""

    def test_admin_guide_no_invite_via_email(self):
        admin_guide = read("docs/admin-guide.md")
        assert "invite other users via email" not in admin_guide, (
            "admin-guide.md still claims invites are sent via email"
        )


# ── DD-2: admin-guide.md invite API table must use correct paths ──

class TestInviteApiPaths:
    """DD-2: admin-guide.md invite table must use /api/organizations/ paths."""

    def test_no_wrong_orgs_path_in_invite_table(self):
        admin_guide = read("docs/admin-guide.md")
        assert "/api/orgs/" not in admin_guide, (
            "admin-guide.md still documents /api/orgs/ — should be /api/organizations/"
        )

    def test_accept_invite_path_has_no_id(self):
        admin_guide = read("docs/admin-guide.md")
        # The accept endpoint should NOT have {id}
        assert "/api/organizations/invites/accept" in admin_guide, (
            "admin-guide.md accept invite path is wrong"
        )


# ── DD-3: admin-guide.md NGINX example must not set X-Forwarded-Prefix ──

class TestNginxForwardedPrefix:
    """DD-3: NGINX example must not set X-Forwarded-Prefix (backend never reads it)."""

    def test_admin_guide_no_forwarded_prefix(self):
        admin_guide = read("docs/admin-guide.md")
        assert "X-Forwarded-Prefix" not in admin_guide, (
            "admin-guide.md still sets X-Forwarded-Prefix in NGINX example — backend never reads it"
        )


# ── A8-8: PORT comment should clarify it's host-side only ──

class TestPortComment:
    """A8-8: .env.example should clarify PORT controls host-side mapping, not in-container listener."""

    def test_env_example_port_comment_documents_fixed_internal(self):
        env_text = read(".env.example")
        port_section = env_text[: env_text.find("APP_ROOT_PATH") or 200]
        assert "9090" in port_section, "PORT section should reference the internal port 9090"
        assert "host" in port_section.lower() or "container" in port_section.lower(), (
            "PORT comment should clarify it controls host-side vs in-container port"
        )
