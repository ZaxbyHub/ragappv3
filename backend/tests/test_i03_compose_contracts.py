"""Issue #750 compose contracts — safe forwards, bounded logs, honest pins.

Four discriminating checks, one per fix lane of the issue:

1. ``test_validated_str_keys_not_forwarded_with_empty_default`` (AC1,
   TQ-sibling-batch-05-01): the five validator/allowlist-constrained ``str``
   keys must never be forwarded as ``KEY=${KEY:-}`` — Settings deliberately
   keeps empty env values meaningful (no ``env_ignore_empty``), so the empty
   default becomes a real ``""`` that breaks the HS-family allowlist at first
   login (JWT_ALGORITHM), the limiter parse (ADMIN_RATE_LIMIT), the vector
   metric validation (VECTOR_METRIC), the IMAP mailbox selection
   (IMAP_MAILBOX) or startup itself (DOCUMENT_PARSING_STRATEGY, fixed first
   in #1061). RED at the issue snapshot for all five.
2. ``test_every_service_bounds_log_growth`` (AC2, T1-04-S-09): every compose
   service runs ``restart: unless-stopped``, so an unbounded json-file log
   grows until the host disk fills. RED at the issue snapshot (no service
   carried a ``logging:`` block).
3. ``test_compose_digest_promise_matches_dependabot_config`` (AC3,
   T1-04-K-05): docker-compose.yml's own comments promise dependabot digest
   PRs; the ``docker`` ecosystem scans Dockerfiles only, so the promise
   requires a ``docker-compose`` ecosystem entry — or the promise text must
   go. RED at the issue snapshot.
4. ``test_windows_launcher_default_image_matches_compose_pin`` (AC4,
   T1-04-MH-12): ``start-services.ps1`` must default to the same
   digest-pinned image compose pins (or resolve it from docker-compose.yml),
   not drift on a floating tag. RED at the issue snapshot.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = ROOT / "docker-compose.yml"
DEPENDABOT_PATH = ROOT / ".github" / "dependabot.yml"
LAUNCHER_PATH = ROOT / "start-services.ps1"

# AC1: every str Settings key with a validator / allowlist / consumer that
# rejects "". DOCUMENT_PARSING_STRATEGY moved to the short form first (#1061);
# the other four follow here (#750).
VALIDATED_STR_KEYS = (
    "JWT_ALGORITHM",
    "ADMIN_RATE_LIMIT",
    "DOCUMENT_PARSING_STRATEGY",
    "VECTOR_METRIC",
    "IMAP_MAILBOX",
)


def _compose_doc() -> dict:
    """yaml.safe_load resolves the x-logging anchors and merge keys."""
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


class TestValidatedStrKeysNotForwardedWithEmptyDefault:
    def test_validated_str_keys_not_forwarded_with_empty_default(self):
        """No service may forward KEY=${KEY:-} for the validated str keys."""
        empty_default_entries = []
        for service, spec in _compose_doc()["services"].items():
            for entry in spec.get("environment") or []:
                if not isinstance(entry, str):
                    continue
                for key in VALIDATED_STR_KEYS:
                    if re.fullmatch(rf"{key}=\$\{{{key}:-\}}", entry):
                        empty_default_entries.append(f"{service}: {entry}")
        self_msg = (
            "validated str keys forwarded with an empty interpolation "
            "default resolve to a real \"\" in Settings (env_ignore_empty "
            "is deliberately off) — use the short form `- KEY` or "
            "`${KEY:-<real default>}` instead: "
            + "; ".join(empty_default_entries)
        )
        assert empty_default_entries == [], self_msg

    def test_validated_str_keys_still_forwarded_to_backend(self):
        """The #494 forwarding contract keeps holding for the five keys.

        AC1 must not be satisfied by deleting the forwards: each key stays
        present in the knowledgevault environment, in short form (omitted
        when unset) or with a non-empty interpolation default.
        """
        env = _compose_doc()["services"]["knowledgevault"]["environment"]
        forwarded = {}
        for entry in env:
            if isinstance(entry, str):
                key, sep, value = entry.partition("=")
                forwarded[key] = value if sep else None
        for key in VALIDATED_STR_KEYS:
            assert key in forwarded, f"{key} disappeared from compose env"
            value = forwarded[key]
            if value is not None:
                default = value.split(":-", 1)[-1].rstrip("}")
                assert default != "", (
                    f"{key}={value} still resolves to \"\" when unset"
                )


class TestEveryServiceBoundsLogGrowth:
    def test_every_service_bounds_log_growth(self):
        unbounded = []
        for service, spec in _compose_doc()["services"].items():
            logging_conf = spec.get("logging") or {}
            driver = logging_conf.get("driver")
            options = logging_conf.get("options") or {}
            if driver not in ("json-file", "local"):
                unbounded.append(
                    f"{service}: logging.driver={driver!r} (need json-file/local)"
                )
            if not options.get("max-size") or not options.get("max-file"):
                unbounded.append(
                    f"{service}: logging.options lacks max-size/max-file"
                )
        assert unbounded == [], (
            "restart: unless-stopped services with unbounded logs can fill "
            "the host disk — attach the x-logging anchor (or an equivalent "
            "json-file/local block with max-size AND max-file): "
            + "; ".join(unbounded)
        )


class TestComposeDigestPromiseMatchesDependabotConfig:
    def test_compose_digest_promise_matches_dependabot_config(self):
        compose_text = COMPOSE_PATH.read_text(encoding="utf-8")
        config = yaml.safe_load(DEPENDABOT_PATH.read_text(encoding="utf-8"))
        has_compose_ecosystem = any(
            update.get("package-ecosystem") == "docker-compose"
            for update in config.get("updates", [])
        )
        # The exact promise phrase the audit flagged (docker-compose.yml's
        # harrier-embed comment). Either the ecosystem backs it or the text
        # must not carry it.
        promise_gone = (
            "dependabot (docker ecosystem) will open PRs" not in compose_text
        )
        assert has_compose_ecosystem or promise_gone, (
            "docker-compose.yml promises dependabot digest PRs for its image "
            "pins, but .github/dependabot.yml has no docker-compose ecosystem "
            "entry (the `docker` ecosystem scans Dockerfiles only) — add the "
            "entry or drop the promise from the comments"
        )

    def test_docker_entries_keep_major_ignore_and_cooldown(self):
        """AC6 (preserving): the docker ecosystem entries keep their
        semver-major ignore and 14-day cooldown when docker-compose is added
        (full pin: test_issue258_build_contracts.py::test_ac9_...)."""
        config = yaml.safe_load(DEPENDABOT_PATH.read_text(encoding="utf-8"))
        docker_updates = [
            u for u in config["updates"] if u.get("package-ecosystem") == "docker"
        ]
        assert docker_updates, "docker ecosystem entries disappeared"
        for update in docker_updates:
            ignored = {
                t for e in update.get("ignore", []) for t in e.get("update-types", [])
            }
            assert "version-update:semver-major" in ignored, update.get("directory")
            assert update.get("cooldown", {}).get("default-days"), (
                update.get("directory")
            )


class TestWindowsLauncherDefaultImageMatchesComposePin:
    def test_windows_launcher_default_image_matches_compose_pin(self):
        compose_image = _compose_doc()["services"]["harrier-embed"]["image"]
        assert "@sha256:" in compose_image, (
            "harrier-embed lost its digest pin — the launcher default check "
            "compares against a pinned reference by design"
        )
        launcher_text = LAUNCHER_PATH.read_text(encoding="utf-8")
        param_line = next(
            line
            for line in launcher_text.splitlines()
            if "$EmbeddingImage" in line and "=" in line
        )
        # Literal-fallback branch: `else { "<image>" }` clauses inside the
        # parameter default expression.
        literal_fallbacks = re.findall(r'else\s*\{\s*"([^"]+)"\s*\}', param_line)
        literal_matches = [
            literal for literal in literal_fallbacks if literal == compose_image
        ]
        # Resolution branch: the default expression itself reads the pin
        # from docker-compose.yml, so it tracks dependabot digest bumps.
        resolves_from_compose = "docker-compose.yml" in param_line
        assert literal_matches or resolves_from_compose, (
            "start-services.ps1's EmbeddingImage default drifts from "
            "docker-compose.yml's harrier-embed digest pin "
            f"({compose_image}): default to that image or resolve it from "
            "docker-compose.yml"
        )
