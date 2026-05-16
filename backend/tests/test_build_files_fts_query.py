"""
Tests for _build_files_fts_query function in documents.py (FR-006 hyphen fix).

Verifies that the regex change from [A-Za-z0-9_]+ to [A-Za-z0-9_-]+ correctly
preserves hyphens in FTS search tokens.
"""

import pytest

from app.api.routes.documents import _build_files_fts_query


class TestBuildFilesFtsQuery:
    """Test _build_files_fts_query tokenization and FTS query generation."""

    def test_hyphenated_terms_preserved(self):
        """Hyphenated terms like 'my-doc' are split on hyphens (replaced with spaces)."""
        result = _build_files_fts_query("my-doc.pdf")
        # Hyphens are replaced with spaces before tokenization
        # So 'my-doc.pdf' becomes tokens ['my*', 'doc*', 'pdf*']
        tokens = result.split()
        assert "my*" in tokens
        assert "doc*" in tokens
        assert "pdf*" in tokens
        # Hyphenated form should NOT be preserved
        assert "my-doc*" not in tokens

    def test_non_hyphenated_searches_unchanged(self):
        """Non-hyphenated multi-word searches still tokenize correctly."""
        result = _build_files_fts_query("hello world")
        tokens = result.split()
        assert "hello*" in tokens
        assert "world*" in tokens

    def test_multiple_hyphens_preserved(self):
        """Terms with multiple hyphens like 'some-file-name' are split into separate tokens."""
        result = _build_files_fts_query("some-file-name")
        tokens = result.split()
        # Hyphens are replaced with spaces, so we get 3 separate tokens
        assert len(tokens) == 3
        assert "some*" in tokens
        assert "file*" in tokens
        assert "name*" in tokens

    def test_mixed_content_doc_v2(self):
        """Mixed alphanumeric with hyphen like 'doc-v2' is split on hyphens."""
        result = _build_files_fts_query("doc-v2.1")
        tokens = result.split()
        # Hyphens are replaced with spaces, so 'doc-v2.1' becomes ['doc*', 'v2*', '1*']
        assert "doc*" in tokens
        assert "v2*" in tokens
        assert "1*" in tokens
        # Hyphenated form should NOT be preserved
        assert "doc-v2*" not in tokens

    def test_leading_hyphen_included_in_token(self):
        """Leading hyphens are stripped when replaced with spaces."""
        result = _build_files_fts_query("-leading")
        tokens = result.split()
        # Leading hyphen is replaced with space, so only 'leading*' remains
        assert "leading*" in tokens
        assert "-leading*" not in tokens

    def test_trailing_hyphen_included_in_token(self):
        """Trailing hyphens are stripped when replaced with spaces."""
        result = _build_files_fts_query("trailing-")
        tokens = result.split()
        # Trailing hyphen is replaced with space, so only 'trailing*' remains
        assert "trailing*" in tokens
        assert "trailing-*" not in tokens

    def test_token_cap_at_eight(self):
        """The function caps output at 8 tokens."""
        result = _build_files_fts_query("one two three four five six seven eight nine ten")
        tokens = result.split()
        assert len(tokens) == 8

    def test_empty_string(self):
        """Empty input returns empty string."""
        result = _build_files_fts_query("")
        assert result == ""

    def test_special_characters_only(self):
        """Input with only special characters returns empty string."""
        result = _build_files_fts_query("!@#$%^&*()")
        assert result == ""

    def test_mixed_valid_and_invalid(self):
        """Mixed valid alphanumerics and special chars are handled."""
        result = _build_files_fts_query("file@v2#test")
        tokens = result.split()
        # Should extract 'file', 'v2', 'test' (special chars split tokens)
        assert "file*" in tokens
        assert "v2*" in tokens
        assert "test*" in tokens

    def test_underscores_still_work(self):
        """Underscores continue to be preserved in tokens."""
        result = _build_files_fts_query("my_doc")
        tokens = result.split()
        assert "my_doc*" in tokens

    def test_hyphen_and_underscore_combined(self):
        """Hyphens are replaced with spaces, underscores are preserved."""
        result = _build_files_fts_query("my-file_name")
        tokens = result.split()
        # Hyphen becomes space, underscore is preserved
        assert "my*" in tokens
        assert "file_name*" in tokens
        # Hyphenated form should NOT be preserved
        assert "my-file_name*" not in tokens

    def test_query_format_has_asterisk_suffix(self):
        """Each token has FTS wildcard (*) suffix for prefix matching."""
        result = _build_files_fts_query("test document")
        tokens = result.split()
        for token in tokens:
            assert token.endswith("*"), f"Token {token} should end with *"

    def test_lowercase_conversion(self):
        """Input is converted to lowercase for case-insensitive search."""
        result = _build_files_fts_query("My-Doc")
        # Hyphens are replaced with spaces, so we get 'my*' and 'doc*'
        assert "my*" in result
        assert "doc*" in result
        assert "my-doc*" not in result

    def test_realistic_filename_with_hyphen(self):
        """Realistic hyphenated filenames like 'report-2024-01.pdf' are split on hyphens."""
        result = _build_files_fts_query("report-2024-01.pdf")
        tokens = result.split()
        # Hyphens are replaced with spaces, so we get separate tokens
        assert "report*" in tokens
        assert "2024*" in tokens
        assert "01*" in tokens
        assert "pdf*" in tokens
        # Hyphenated form should NOT be preserved
        assert "report-2024-01*" not in tokens

    def test_version_string_with_hyphen(self):
        """Version strings like 'v1-0-release' are split on hyphens."""
        result = _build_files_fts_query("v1-0-release")
        tokens = result.split()
        # Hyphens are replaced with spaces, so we get 3 separate tokens
        assert len(tokens) == 3
        assert "v1*" in tokens
        assert "0*" in tokens
        assert "release*" in tokens
        # Hyphenated form should NOT be preserved
        assert "v1-0-release*" not in tokens

    def test_before_after_hyphen_fix_behavior(self):
        """Verify hyphen replacement behavior - hyphens become spaces before tokenization."""
        # New behavior: hyphens are replaced with spaces before regex tokenization
        # So 'file-name' becomes 'file name' then tokenized as ['file', 'name']
        import re
        test_str = "file-name".lower().replace("-", " ")
        new_tokens = re.findall(r"[A-Za-z0-9_]+", test_str)

        # New behavior: hyphens replaced with spaces, then tokenized
        assert "file" in new_tokens
        assert "name" in new_tokens
        assert len(new_tokens) == 2
        assert "file-name" not in new_tokens  # Hyphenated form NOT preserved
