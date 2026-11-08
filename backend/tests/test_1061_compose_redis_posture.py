"""Issue #1061 durable guard: the bundled compose Redis keeps its auth posture.

RED at base (the shipped compose file published ``6379:6379`` on all host
interfaces with no ``--requirepass``, an unauthenticated healthcheck, and a
credential-free backend REDIS_URL default), GREEN after the fix.

Structural (YAML) assertions over the SOURCE compose file — no docker daemon
needed, so this runs in the ordinary Backend CI job. The source file uses
string-form commands, list-form environment entries, and ``${VAR:-default}``
interpolation. Compose substitutes the SAME expression text into every site,
so the credential-coordination assertions compare the raw expression tokens
(exact by construction); non-emptiness additionally requires the expression's
own default (after ``:-``) to be non-empty:
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
    def test_requirepass_with_nonempty_default(self):
        assert _requirepass_token()

    def test_healthcheck_uses_same_credential_token(self):
        token = _requirepass_token()
        test = _services()["redis"]["healthcheck"]["test"]
        assert "-a" in test, "healthcheck must authenticate (bare redis-cli ping NOAUTHs under requirepass)"
        assert test[test.index("-a") + 1] == token, "healthcheck credential must match requirepass"

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
