"""CSRF test-policy declaration contract (issue #202 / ENH-008, AC2).

Pins the behavior of conftest._module_manages_csrf: an explicit module-level
``CSRF_TEST_POLICY = "naive" | "manages"`` declaration overrides the incidental
lexical scan, an unrecognized value falls through, and the legacy substring
default is preserved for undeclared modules.
"""

import conftest  # noqa: E402  (repo precedent: test_agentic_control_parity)


def _classify(source: str) -> bool:
    """Write `source` to a throwaway module and run the real classifier on it."""
    import os
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".py", prefix="csrf_policy_case_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(source)
        conftest._CSRF_AWARE_MODULES.clear()
        return conftest._module_manages_csrf(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_naive_declaration_overrides_incidental_mentions():
    """A declared-naive module is bypassed even when its text mentions csrf."""
    source = (
        '"""Suite that is CSRF-naive (declared) despite prose mentions."""\n'
        "\n"
        "# mentions csrf and _csrf_token in prose only.\n"
        'CSRF_TEST_POLICY = "naive"\n'
        "\n"
        "def test_placeholder():\n"
        "    pass\n"
    )
    assert _classify(source) is False


def test_manages_declaration_forces_enforcement():
    """A declared-manages module stays CSRF-aware."""
    source = (
        '"""Suite that manages enforcement itself (declared)."""\n'
        "\n"
        'CSRF_TEST_POLICY = "manages"\n'
        "\n"
        "def test_placeholder():\n"
        "    pass\n"
    )
    assert _classify(source) is True


def test_manages_declaration_with_other_mentions():
    """A declared-manages module is aware even with extra incidental mentions."""
    source = (
        '"""Suite that manages CSRF itself."""\n'
        "\n"
        "# references csrf_protect and the _csrf_token parameter.\n"
        'CSRF_TEST_POLICY = "manages"\n'
        "\n"
        "def test_placeholder():\n"
        "    pass\n"
    )
    assert _classify(source) is True


def test_no_declaration_keeps_legacy_substring_default():
    """Without a declaration, a source mentioning csrf stays classified aware."""
    source = (
        '"""Legacy suite with no declaration."""\n'
        "\n"
        "# mentions csrf and _csrf_token in prose.\n"
        "\n"
        "def test_placeholder():\n"
        "    pass\n"
    )
    assert _classify(source) is True


def test_no_declaration_and_no_mention_is_naive():
    """Without a declaration or any mention, the module gets the bypass."""
    source = (
        '"""Suite with no declaration and no mention."""\n'
        "\n"
        "def test_placeholder():\n"
        "    pass\n"
    )
    assert _classify(source) is False


def test_unrecognized_value_falls_through_to_legacy_scan():
    """An unknown declaration value is not honored as either polarity.

    The constant name itself matches the case-insensitive scan, so this
    module classifies as aware via the legacy fallback — pinning that a
    future 'reject unknown values' tightening cannot silently flip modules.
    """
    source = (
        '"""Suite with an unrecognized declaration value."""\n'
        "\n"
        'CSRF_TEST_POLICY = "NATIVE"\n'
        "\n"
        "def test_placeholder():\n"
        "    pass\n"
    )
    assert _classify(source) is True


def test_declaration_with_trailing_comment_is_recognized():
    """The declaration grammar accepts a trailing comment."""
    source = (
        '"""Suite that is CSRF-naive (declared with a comment)."""\n'
        "\n"
        "# mentions csrf in prose.\n"
        'CSRF_TEST_POLICY = "naive"  # this suite never exercises enforcement\n'
        "\n"
        "def test_placeholder():\n"
        "    pass\n"
    )
    assert _classify(source) is False
