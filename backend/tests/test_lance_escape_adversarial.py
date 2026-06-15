"""Adversarial tests for _lance_escape() SQL injection defense.

Covers: integer passthrough, string passthrough, quote-doubling for various
injection patterns (classic SQLi, UNION, comment termination), empty string,
None, and a realistic filter-context assertion.
"""

import pytest

from app.services.vector_store import _lance_escape


class TestLanceEscapeBasics:
    """Happy-path / boundary tests."""

    def test_normal_integer_passthrough(self):
        """Integer is converted to string unchanged."""
        assert _lance_escape(42) == "42"

    def test_normal_string_passthrough(self):
        """Plain string with no quotes passes through unchanged."""
        assert _lance_escape("42") == "42"

    def test_empty_string(self):
        """Empty string remains empty after escaping."""
        assert _lance_escape("") == ""

    def test_none_value(self):
        """None is converted to the string 'None'."""
        assert _lance_escape(None) == "None"


class TestLanceEscapeSqlInjectionDefenses:
    """SQL injection patterns — all single quotes must be doubled."""

    def test_single_quote_escaped(self):
        """Classic tautology injection: 1' OR '1'='1 -> 1'' OR ''1''=''1."""
        assert _lance_escape("1' OR '1'='1") == "1'' OR ''1''=''1"

    def test_classic_sql_injection(self):
        """DROP TABLE injection: '; DROP TABLE memories; --."""
        assert _lance_escape("'; DROP TABLE memories; --") == "''; DROP TABLE memories; --"

    def test_union_injection(self):
        """UNION-based data extraction: 1' UNION SELECT * FROM memories--."""
        result = _lance_escape("1' UNION SELECT * FROM memories--")
        assert result == "1'' UNION SELECT * FROM memories--"

    def test_comment_termination(self):
        """MySQL-style comment termination: 1'--."""
        assert _lance_escape("1'--") == "1''--"

    def test_multiple_quotes(self):
        """Arbitrary sequence of quotes: 'a'b'c' -> ''a''b''c''."""
        assert _lance_escape("'a'b'c'") == "''a''b''c''"


class TestLanceEscapeFilterContextDefense:
    """Verify escaping actually prevents predicate injection in filter strings."""

    def test_filter_context_prevents_or_predicate_injection(self):
        """An OR-style vault_id payload cannot break out of the string literal.

        Without escaping, a vault_id of ``1' OR vault_id != '`` would produce:
            vault_id = '1' OR vault_id != '
        which LanceDB parses as a predicate (OR injection), not a string value.

        With escaping it becomes:
            vault_id = '1'' OR vault_id != ''
        which LanceDB parses as the literal string ``1' OR vault_id != '`` —
        the embedded quotes are data, not syntax.
        """
        malicious = "1' OR vault_id != '"
        escaped = _lance_escape(malicious)
        filter_str = f"vault_id = '{escaped}'"

        # The filter string must have the exact expected shape:
        assert filter_str == "vault_id = '1'' OR vault_id != '''"

        # Key invariant: every single-quote from the original input now appears
        # as a doubled '' pair in the escaped value, so none can act as a string
        # terminator.  Verify by checking that the escaped value has exactly
        # 2x the quote count of the original (all quotes doubled).
        assert escaped.count("'") == malicious.count("'") * 2

    def test_quotes_from_input_are_all_doubled(self):
        """Verify the core invariant: every quote in input → '' pair in output.

        This is the fundamental property that prevents injection, regardless of
        what the surrounding filter context looks like.
        """
        Q = "'"
        cases = [
            ("a'b", 1),           # 1 quote -> 2
            ("'", 1),             # 1 -> 2
            ("''", 2),            # 2 -> 4
            ("1' OR '1'='1", 4),  # 4 quotes -> 8
            ("'; DROP TABLE", 1), # 1 -> 2
        ]
        for original, expected_quotes in cases:
            escaped = _lance_escape(original)
            actual = escaped.count(Q)
            expected = expected_quotes * 2
            assert actual == expected, (
                f"Input {original!r} (quotes={expected_quotes}) "
                f"escaped to {escaped!r} (quotes={actual}) — expected {expected}"
            )

    def test_double_quote_is_not_affected(self):
        """Double quotes are not SQL-injection relevant here and must be preserved."""
        result = _lance_escape('a"b"c')
        assert result == 'a"b"c'
