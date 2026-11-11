"""Issue #1061 durable guard: the bundled compose Redis keeps its auth posture.

RED at base (the shipped compose file published ``6379:6379`` on all host
interfaces with no ``--requirepass``, an unauthenticated healthcheck, and a
credential-free backend REDIS_URL default), GREEN after the fix.

Structural (YAML) assertions over the SOURCE compose file — no docker daemon
needed, so this runs in the ordinary Backend CI job. The source file uses
string-form commands, list-form environment entries, and ``${VAR:-default}``
interpolation. Compose substitutes the SAME expression text into every site,
so the credential-coordination assertions compare the raw expression tokens
(exact by construction); the healthcheck reads the credential from the
service's runtime environment ($$-escaped) rather than from a composed shell
string, and non-emptiness additionally requires the expression's own default
(after ``:-``) to be non-empty:
- the redis service command carries ``--requirepass`` with a value whose
  unset-default is non-empty;
- the healthcheck probe authenticates with the SAME token;
- the backend ``REDIS_URL`` default carries the SAME token as its URL
  password segment;
- every published redis port is loopback-bound and at least one is published
  (the adjudicated direction KEEPS a loopback publication; full unpublish
  requires explicit user sign-off).

This is the shipped rung for the #1061 redis posture — the acceptance check
that certified the fix lives in the git-excluded issue-trace directory and
guards nothing once the trace is archived.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yml"

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _services() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]


def _redis_command() -> list:
    cmd = _services()["redis"]["command"]
    return cmd.split() if isinstance(cmd, str) else list(cmd)


def _healthcheck_shell() -> str:
    test = _services()["redis"]["healthcheck"]["test"]
    if isinstance(test, list):
        # CMD-SHELL carries the whole shell line as one element; join so the
        # token/pipe assertions work for both list and plain-string shapes.
        return " ".join(str(part) for part in test)
    return str(test)


def _requirepass_token() -> str:
    cmd = _redis_command()
    assert "--requirepass" in cmd, "redis command must carry --requirepass"
    token = cmd[cmd.index("--requirepass") + 1]
    assert token, "--requirepass value must be present"
    # STRICT FORM: every credential site must use ${VAR:-nonempty-default}.
    # Rejected: ${VAR} bare and unbraced $VAR (resolve empty when unset ->
    # crash-loop redis), ${VAR-default} (single dash resolves empty the same
    # way), and empty defaults.
    assert re.fullmatch(r"\$\{[A-Z][A-Z0-9_]*:-[^}]+\}", token), (
        "--requirepass must use the ${VAR:-nonempty} form "
        f"(got {token!r}; unset-empty forms crash-loop redis)"
    )
    return token


class TestRedisRequiresAuthentication:
    def test_command_is_list_form(self):
        cmd = _services()["redis"]["command"]
        assert isinstance(cmd, list), (
            "redis command must be a LIST (compose passes lists to the "
            "container without a shell; a string command would shell-parse an "
            "operator password and can truncate the --requirepass value)"
        )

    def test_requirepass_with_nonempty_default(self):
        assert _requirepass_token()

    def test_healthcheck_reads_runtime_env_not_interpolation(self):
        """The healthcheck must read the password from the container
        environment ($$REDIS_PASSWORD at runtime) — composing the credential
        INTO the shell string would let a password containing a single quote
        break out of the quoted -a argument (shell injection)."""
        shell = _healthcheck_shell()
        assert '-a "$$REDIS_PASSWORD"' in shell, (
            'healthcheck must authenticate via the runtime $REDIS_PASSWORD '
            'env var (escaped $$ so compose does not interpolate the '
            'credential into the shell string)'
        )

    def test_redis_service_carries_the_runtime_env(self):
        env = _services()["redis"].get("environment") or {}
        raw = env.get("REDIS_PASSWORD") if isinstance(env, dict) else None
        if raw is None and not isinstance(env, dict):
            raw = next((e.split("=", 1)[1] for e in env
                        if isinstance(e, str) and e.startswith("REDIS_PASSWORD=")), None)
        token = _requirepass_token()
        assert raw is not None, "redis service must export REDIS_PASSWORD to the container env"
        # The REAL agreement invariant: the env token expression must EQUAL the
        # --requirepass token expression (the M3 mutant — a divergent env
        # default — breaks the deployed stack's healthcheck whenever the host
        # var is unset, while every CI gate stays green).
        assert raw == token, (
            f"service env REDIS_PASSWORD token {raw!r} must equal the "
            f"--requirepass token {token!r} (divergence breaks the healthcheck "
            "when the host var is unset)"
        )

    def test_backend_redis_url_override_wins_by_construction(self):
        """The operator's non-empty REDIS_URL must win verbatim: the entry
        must keep the ${REDIS_URL:-...} fallback shape (compose semantics)."""
        env = _services()["knowledgevault"]["environment"]
        if isinstance(env, dict):
            raw = env["REDIS_URL"]
        else:
            raw = next(e for e in env if e.startswith("REDIS_URL="))
        assert "REDIS_URL=${REDIS_URL:-redis://" in raw, (
            "REDIS_URL must forward as ${REDIS_URL:-default} so a non-empty "
            "operator override wins verbatim"
        )

    def test_healthcheck_greps_pong(self):
        """redis-cli exits 0 even on a WRONG password (AUTH failed), so only a
        PONG grep fails the healthcheck on a credential mismatch."""
        shell = _healthcheck_shell()
        assert "grep -q PONG" in shell, (
            "healthcheck must pipe ping through 'grep -q PONG' — the bare "
            "command exits 0 on WRONGPASS and cannot detect a credential "
            "mismatch"
        )

    def test_backend_redis_url_carries_same_credential_token(self):
        token = _requirepass_token()
        env = _services()["knowledgevault"]["environment"]
        if isinstance(env, dict):
            raw = env["REDIS_URL"]
        else:
            raw = next(e.split("=", 1)[1] for e in env if e.startswith("REDIS_URL="))
        m = re.search(r"redis://:([^@]+)@", raw)
        assert m, f"backend REDIS_URL default carries no password segment: {raw!r}"
        assert m.group(1) == token, "backend REDIS_URL password segment must match requirepass"


class TestRedisPublishIsLoopbackOnly:
    def test_every_publication_loopback_bound_and_at_least_one(self):
        ports = _services()["redis"].get("ports") or []
        assert ports, "redis must keep at least a loopback publication (unpublish needs explicit sign-off)"
        for p in ports:
            # Source form is a string "host:container" / "ip:host:container";
            # resolved dict form carries host_ip.
            if isinstance(p, dict):
                assert p.get("host_ip") in LOOPBACK, f"redis publish must be loopback-bound: {p}"
                continue
            parts = str(p).split(":")
            if len(parts) == 3:
                assert parts[0] in LOOPBACK, f"redis publish must be loopback-bound: {p}"
            else:
                # len == 2 ("6379:6379") publishes on ALL interfaces — reject.
                raise AssertionError(f"redis publish binds all host interfaces: {p}")

    def test_compose_source_has_no_all_interface_publish(self):
        text = COMPOSE.read_text(encoding="utf-8")
        assert not re.search(r'-\s*"6379:6379"', text), (
            'publishing "6379:6379" binds all host interfaces — use "127.0.0.1:6379:6379"'
        )
