"""Issue #1061 review-round pin: the release runbook's Redis probe keeps its
credential contract.

The probe has shipped three real defects in its first two rounds (credential
echo through a fail-open sanitizer, a redis-cli URL form the server rejects,
argv credential exposure), all invisible to CI because CI never runs the
runbook. This pin is a doc-content contract on docs/release.md's section-5
block: the probe must (a) authenticate through REDISCLI_AUTH — never ``-u``
(argv) — (b) exit nonzero on failure, (c) fail closed (redact, never echo) on
an unparseable URL, and (d) strip quotes/CR/export prefixes from .env values
the way compose does.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RELEASE = ROOT / "docs" / "release.md"


def probe_block() -> str:
    text = RELEASE.read_text(encoding="utf-8")
    m = re.search(r"#### 5\. Redis Connection Test.*?```bash\n(.*?)```", text, re.S)
    assert m, "release.md section 5 (Redis Connection Test) not found"
    return m.group(1)


class TestReleaseProbeCredentialContract:
    def test_authenticates_via_rediscli_auth_not_argv(self):
        block = probe_block()
        assert "REDISCLI_AUTH" in block, (
            "the probe must pass the credential through the REDISCLI_AUTH "
            "environment variable (never argv)"
        )
        assert not re.search(r"redis-cli[^\n]*-u\b", block), (
            "the probe must not pass a credential-bearing URL via redis-cli -u: "
            "the empty-username form sends a two-arg AUTH the server rejects, "
            "and -u places the credential in argv"
        )

    def test_exits_nonzero_on_failure(self):
        block = probe_block()
        assert re.search(r"Redis FAILED[\s\S]{0,40}?exit 1", block), (
            "the probe must exit 1 on the failure arm so automation can gate on it"
        )
        assert "exit 0" in block, "the success arm should exit 0 explicitly"

    def test_unparseable_url_is_redacted_not_echoed(self):
        block = probe_block()
        assert "redacted" in block and "exit 1" in block, (
            "an unparseable REDIS_URL must be printed redacted and fail the "
            "probe (fail closed), never echoed"
        )

    def test_env_values_normalized_like_compose(self):
        block = probe_block()
        assert "export[[:space:]]+" in block, (
            "the .env loader must accept compose-style `export `-prefixed lines"
        )
        assert "tr -d '\\r'" in block, "the .env loader must strip CRLF carriage returns"
        # KEY= prefix stripped BEFORE the quote expressions (a quoted value
        # containing '=' must survive; the quotes-then-key order truncated
        # base64-padded values to '=').
        key_strip = block.find("s/^[^=]*=//")
        quote_strip = block.find('s/^"(.*)"$/')
        assert key_strip != -1 and quote_strip != -1 and key_strip < quote_strip, (
            "the .env loader must strip the KEY= prefix before stripping quotes"
        )

    def test_unsupported_url_forms_rejected_redacted(self):
        """rediss:// TLS, IPv6 literals, ACL usernames, and out-of-alphabet
        credentials are refused redacted with a nonzero exit — never
        mis-reported as OK over the wrong connection kind."""
        block = probe_block()
        assert "not supported by this probe" in block, (
            "the probe must fail closed (redacted) on REDIS_URL forms it "
            "cannot represent: non-redis schemes, IPv6, ACL users, "
            "out-of-alphabet credentials"
        )
        assert "REDISCLI_AUTH" in block  # auth form stays env-based
