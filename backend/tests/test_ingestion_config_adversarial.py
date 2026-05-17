"""
Adversarial security tests for the 5 new ingestion performance config fields.

Tests cover attack vectors ONLY:
- ingestion_worker_count: int (1-16) — test 0, -1, 17, 100, "abc", None, 3.14
- optimize_mode: str (after_every_write/periodic/manual) — test "invalid", "", "PERIODIC",
  " periodic ", None, "after_every_write\n"
- optimize_interval_chunks: int (>=1) — test 0, -1, -100, "abc", None
- embedding_concurrent_batches: int (1-16) — test 0, -1, 17, "abc", None
- optimize_on_shutdown: bool — test "true", "false", 1, 0, None, "yes"
"""

from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.config import Settings


# =============================================================================
# ingestion_worker_count: int (1-16) — boundary + type confusion attacks
# =============================================================================

class TestIngestionWorkerCountAdversarial:
    """Adversarial tests for ingestion_worker_count (range 1-16)."""

    def test_valid_boundary_values(self):
        """Valid boundary values 1 and 16 should be accepted."""
        with patch.dict("os.environ", {"INGESTION_WORKER_COUNT": "1"}):
            s = Settings()
            assert s.ingestion_worker_count == 1

        with patch.dict("os.environ", {"INGESTION_WORKER_COUNT": "16"}):
            s = Settings()
            assert s.ingestion_worker_count == 16

    def test_valid_mid_range(self):
        """Valid mid-range value should be accepted."""
        with patch.dict("os.environ", {"INGESTION_WORKER_COUNT": "8"}):
            s = Settings()
            assert s.ingestion_worker_count == 8

    def test_rejects_zero(self):
        """Zero should be rejected — below minimum of 1."""
        with patch.dict("os.environ", {"INGESTION_WORKER_COUNT": "0"}):
            with pytest.raises(ValidationError) as exc:
                Settings()
            assert "ingestion_worker_count must be >= 1" in str(exc.value)

    def test_rejects_negative(self):
        """Negative value should be rejected."""
        with patch.dict("os.environ", {"INGESTION_WORKER_COUNT": "-1"}):
            with pytest.raises(ValidationError) as exc:
                Settings()
            assert "ingestion_worker_count must be >= 1" in str(exc.value)

    def test_rejects_large_negative(self):
        """Large negative value should be rejected."""
        with patch.dict("os.environ", {"INGESTION_WORKER_COUNT": "-100"}):
            with pytest.raises(ValidationError) as exc:
                Settings()
            assert "ingestion_worker_count must be >= 1" in str(exc.value)

    def test_rejects_above_max(self):
        """Value above 16 should be rejected."""
        with patch.dict("os.environ", {"INGESTION_WORKER_COUNT": "17"}):
            with pytest.raises(ValidationError) as exc:
                Settings()
            assert "ingestion_worker_count must be <= 16" in str(exc.value)

    def test_rejects_extremely_large(self):
        """Extremely large value (100) should be rejected."""
        with patch.dict("os.environ", {"INGESTION_WORKER_COUNT": "100"}):
            with pytest.raises(ValidationError) as exc:
                Settings()
            assert "ingestion_worker_count must be <= 16" in str(exc.value)

    def test_rejects_non_numeric_string(self):
        """Non-numeric string 'abc' should be rejected (type confusion)."""
        with patch.dict("os.environ", {"INGESTION_WORKER_COUNT": "abc"}):
            with pytest.raises(ValidationError) as exc:
                Settings()
            # Pydantic will raise a type error for int conversion failure
            assert "ingestion_worker_count" in str(exc.value).lower()

    def test_rejects_float_string(self):
        """Float string '3.14' should be rejected (type confusion)."""
        with patch.dict("os.environ", {"INGESTION_WORKER_COUNT": "3.14"}):
            with pytest.raises(ValidationError):
                Settings()

    def test_rejects_none(self):
        """None should be rejected (type confusion — explicit None is not valid input)."""
        with patch.dict("os.environ", {"INGESTION_WORKER_COUNT": "None"}):
            with pytest.raises(ValidationError):
                Settings()

    def test_rejects_whitespace_only(self):
        """Whitespace-only string should be rejected."""
        with patch.dict("os.environ", {"INGESTION_WORKER_COUNT": "   "}):
            with pytest.raises(ValidationError):
                Settings()

    def test_rejects_empty_string(self):
        """Empty string should be rejected."""
        with patch.dict("os.environ", {"INGESTION_WORKER_COUNT": ""}):
            with pytest.raises(ValidationError):
                Settings()


# =============================================================================
# optimize_mode: str (after_every_write/periodic/manual) — enum attacks
# =============================================================================

class TestOptimizeModeAdversarial:
    """Adversarial tests for optimize_mode enum validation."""

    def test_valid_values(self):
        """All three valid values should be accepted."""
        for val in ("after_every_write", "periodic", "manual"):
            with patch.dict("os.environ", {"OPTIMIZE_MODE": val}):
                s = Settings()
                assert s.optimize_mode == val

    def test_rejects_invalid_value(self):
        """Invalid value 'invalid' should be rejected."""
        with patch.dict("os.environ", {"OPTIMIZE_MODE": "invalid"}):
            with pytest.raises(ValidationError) as exc:
                Settings()
            assert "optimize_mode must be one of" in str(exc.value)

    def test_rejects_empty_string(self):
        """Empty string should be rejected."""
        with patch.dict("os.environ", {"OPTIMIZE_MODE": ""}):
            with pytest.raises(ValidationError) as exc:
                Settings()
            assert "optimize_mode must be one of" in str(exc.value)

    def test_rejects_uppercase_variant(self):
        """Uppercase 'PERIODIC' should be rejected (case-sensitive)."""
        with patch.dict("os.environ", {"OPTIMIZE_MODE": "PERIODIC"}):
            with pytest.raises(ValidationError) as exc:
                Settings()
            assert "optimize_mode must be one of" in str(exc.value)

    def test_rejects_mixed_case(self):
        """Mixed-case variant should be rejected (case-sensitive)."""
        with patch.dict("os.environ", {"OPTIMIZE_MODE": "Periodic"}):
            with pytest.raises(ValidationError) as exc:
                Settings()
            assert "optimize_mode must be one of" in str(exc.value)

    def test_rejects_value_with_leading_trailing_whitespace(self):
        """Value with leading/trailing whitespace should be rejected."""
        with patch.dict("os.environ", {"OPTIMIZE_MODE": " periodic "}):
            with pytest.raises(ValidationError) as exc:
                Settings()
            assert "optimize_mode must be one of" in str(exc.value)

    def test_rejects_value_with_newline(self):
        """Value with embedded newline should be rejected."""
        with patch.dict("os.environ", {"OPTIMIZE_MODE": "after_every_write\n"}):
            with pytest.raises(ValidationError) as exc:
                Settings()
            assert "optimize_mode must be one of" in str(exc.value)

    def test_rejects_value_with_tab(self):
        """Value with embedded tab should be rejected."""
        with patch.dict("os.environ", {"OPTIMIZE_MODE": "periodic\t"}):
            with pytest.raises(ValidationError) as exc:
                Settings()
            assert "optimize_mode must be one of" in str(exc.value)

    def test_rejects_none_literal(self):
        """None literal should be rejected."""
        with patch.dict("os.environ", {"OPTIMIZE_MODE": "None"}):
            with pytest.raises(ValidationError):
                Settings()

    def test_rejects_numeric_string(self):
        """Numeric string '123' should be rejected (type confusion)."""
        with patch.dict("os.environ", {"OPTIMIZE_MODE": "123"}):
            with pytest.raises(ValidationError) as exc:
                Settings()
            assert "optimize_mode must be one of" in str(exc.value)

    def test_rejects_sql_injection_attempt(self):
        """SQL injection-like value should be rejected."""
        with patch.dict("os.environ", {"OPTIMIZE_MODE": "'; DROP TABLE--"}):
            with pytest.raises(ValidationError) as exc:
                Settings()
            assert "optimize_mode must be one of" in str(exc.value)

    def test_rejects_template_injection_attempt(self):
        """Template literal injection attempt should be rejected."""
        with patch.dict("os.environ", {"OPTIMIZE_MODE": "${env:ADMIN_SECRET}"}):
            with pytest.raises(ValidationError):
                Settings()


# =============================================================================
# optimize_interval_chunks: int (>=1) — boundary + type confusion attacks
# =============================================================================

class TestOptimizeIntervalChunksAdversarial:
    """Adversarial tests for optimize_interval_chunks (>=1)."""

    def test_valid_values(self):
        """Valid values 1 and large integer should be accepted."""
        with patch.dict("os.environ", {"OPTIMIZE_INTERVAL_CHUNKS": "1"}):
            s = Settings()
            assert s.optimize_interval_chunks == 1

        with patch.dict("os.environ", {"OPTIMIZE_INTERVAL_CHUNKS": "5000"}):
            s = Settings()
            assert s.optimize_interval_chunks == 5000

        with patch.dict("os.environ", {"OPTIMIZE_INTERVAL_CHUNKS": "100000"}):
            s = Settings()
            assert s.optimize_interval_chunks == 100000

    def test_rejects_zero(self):
        """Zero should be rejected — minimum is 1."""
        with patch.dict("os.environ", {"OPTIMIZE_INTERVAL_CHUNKS": "0"}):
            with pytest.raises(ValidationError) as exc:
                Settings()
            assert "optimize_interval_chunks must be >= 1" in str(exc.value)

    def test_rejects_negative_one(self):
        """-1 should be rejected."""
        with patch.dict("os.environ", {"OPTIMIZE_INTERVAL_CHUNKS": "-1"}):
            with pytest.raises(ValidationError) as exc:
                Settings()
            assert "optimize_interval_chunks must be >= 1" in str(exc.value)

    def test_rejects_large_negative(self):
        """Large negative value -100 should be rejected."""
        with patch.dict("os.environ", {"OPTIMIZE_INTERVAL_CHUNKS": "-100"}):
            with pytest.raises(ValidationError) as exc:
                Settings()
            assert "optimize_interval_chunks must be >= 1" in str(exc.value)

    def test_rejects_non_numeric_string(self):
        """Non-numeric string 'abc' should be rejected (type confusion)."""
        with patch.dict("os.environ", {"OPTIMIZE_INTERVAL_CHUNKS": "abc"}):
            with pytest.raises(ValidationError) as exc:
                Settings()
            assert "optimize_interval_chunks" in str(exc.value).lower()

    def test_rejects_float_string(self):
        """Float string should be rejected."""
        with patch.dict("os.environ", {"OPTIMIZE_INTERVAL_CHUNKS": "3.14"}):
            with pytest.raises(ValidationError):
                Settings()

    def test_rejects_none(self):
        """None literal should be rejected."""
        with patch.dict("os.environ", {"OPTIMIZE_INTERVAL_CHUNKS": "None"}):
            with pytest.raises(ValidationError):
                Settings()

    def test_rejects_empty_string(self):
        """Empty string should be rejected."""
        with patch.dict("os.environ", {"OPTIMIZE_INTERVAL_CHUNKS": ""}):
            with pytest.raises(ValidationError):
                Settings()


# =============================================================================
# embedding_concurrent_batches: int (1-16) — boundary + type confusion attacks
# =============================================================================

class TestEmbeddingConcurrentBatchesAdversarial:
    """Adversarial tests for embedding_concurrent_batches (range 1-16)."""

    def test_valid_boundary_values(self):
        """Valid boundary values 1 and 16 should be accepted."""
        with patch.dict("os.environ", {"EMBEDDING_CONCURRENT_BATCHES": "1"}):
            s = Settings()
            assert s.embedding_concurrent_batches == 1

        with patch.dict("os.environ", {"EMBEDDING_CONCURRENT_BATCHES": "16"}):
            s = Settings()
            assert s.embedding_concurrent_batches == 16

    def test_valid_mid_range(self):
        """Valid mid-range value should be accepted."""
        with patch.dict("os.environ", {"EMBEDDING_CONCURRENT_BATCHES": "8"}):
            s = Settings()
            assert s.embedding_concurrent_batches == 8

    def test_rejects_zero(self):
        """Zero should be rejected — below minimum of 1."""
        with patch.dict("os.environ", {"EMBEDDING_CONCURRENT_BATCHES": "0"}):
            with pytest.raises(ValidationError) as exc:
                Settings()
            assert "embedding_concurrent_batches must be >= 1" in str(exc.value)

    def test_rejects_negative(self):
        """Negative value should be rejected."""
        with patch.dict("os.environ", {"EMBEDDING_CONCURRENT_BATCHES": "-1"}):
            with pytest.raises(ValidationError) as exc:
                Settings()
            assert "embedding_concurrent_batches must be >= 1" in str(exc.value)

    def test_rejects_above_max(self):
        """Value above 16 should be rejected."""
        with patch.dict("os.environ", {"EMBEDDING_CONCURRENT_BATCHES": "17"}):
            with pytest.raises(ValidationError) as exc:
                Settings()
            assert "embedding_concurrent_batches must be <= 16" in str(exc.value)

    def test_rejects_extremely_large(self):
        """Extremely large value should be rejected."""
        with patch.dict("os.environ", {"EMBEDDING_CONCURRENT_BATCHES": "100"}):
            with pytest.raises(ValidationError) as exc:
                Settings()
            assert "embedding_concurrent_batches must be <= 16" in str(exc.value)

    def test_rejects_non_numeric_string(self):
        """Non-numeric string 'abc' should be rejected (type confusion)."""
        with patch.dict("os.environ", {"EMBEDDING_CONCURRENT_BATCHES": "abc"}):
            with pytest.raises(ValidationError) as exc:
                Settings()
            assert "embedding_concurrent_batches" in str(exc.value).lower()

    def test_rejects_float_string(self):
        """Float string should be rejected (type confusion)."""
        with patch.dict("os.environ", {"EMBEDDING_CONCURRENT_BATCHES": "3.14"}):
            with pytest.raises(ValidationError):
                Settings()

    def test_rejects_none(self):
        """None literal should be rejected."""
        with patch.dict("os.environ", {"EMBEDDING_CONCURRENT_BATCHES": "None"}):
            with pytest.raises(ValidationError):
                Settings()

    def test_rejects_empty_string(self):
        """Empty string should be rejected."""
        with patch.dict("os.environ", {"EMBEDDING_CONCURRENT_BATCHES": ""}):
            with pytest.raises(ValidationError):
                Settings()


# =============================================================================
# optimize_on_shutdown: bool — type confusion + injection attacks
# =============================================================================

class TestOptimizeOnShutdownAdversarial:
    """Adversarial tests for optimize_on_shutdown (bool)."""

    def test_accepts_true_boolean(self):
        """True boolean should be accepted."""
        with patch.dict("os.environ", {"OPTIMIZE_ON_SHUTDOWN": "true"}):
            s = Settings()
            assert s.optimize_on_shutdown is True

    def test_accepts_false_boolean(self):
        """False boolean should be accepted."""
        with patch.dict("os.environ", {"OPTIMIZE_ON_SHUTDOWN": "false"}):
            s = Settings()
            assert s.optimize_on_shutdown is False

    def test_accepts_one(self):
        """Integer 1 should be coerced to True."""
        with patch.dict("os.environ", {"OPTIMIZE_ON_SHUTDOWN": "1"}):
            s = Settings()
            assert s.optimize_on_shutdown is True

    def test_accepts_zero(self):
        """Integer 0 should be coerced to False."""
        with patch.dict("os.environ", {"OPTIMIZE_ON_SHUTDOWN": "0"}):
            s = Settings()
            assert s.optimize_on_shutdown is False

    def test_accepts_uppercase_true(self):
        """Uppercase 'TRUE' should be coerced to True."""
        with patch.dict("os.environ", {"OPTIMIZE_ON_SHUTDOWN": "TRUE"}):
            s = Settings()
            assert s.optimize_on_shutdown is True

    def test_accepts_uppercase_false(self):
        """Uppercase 'FALSE' should be coerced to False."""
        with patch.dict("os.environ", {"OPTIMIZE_ON_SHUTDOWN": "FALSE"}):
            s = Settings()
            assert s.optimize_on_shutdown is False

    def test_accepts_yes_coerced_to_true(self):
        """'yes' is coerced to True (Pydantic v2 strtobool behavior — intentional)."""
        with patch.dict("os.environ", {"OPTIMIZE_ON_SHUTDOWN": "yes"}):
            s = Settings()
            assert s.optimize_on_shutdown is True

    def test_accepts_no_coerced_to_false(self):
        """'no' is coerced to False (Pydantic v2 strtobool behavior — intentional)."""
        with patch.dict("os.environ", {"OPTIMIZE_ON_SHUTDOWN": "no"}):
            s = Settings()
            assert s.optimize_on_shutdown is False

    def test_accepts_on_coerced_to_true(self):
        """'on' is coerced to True (Pydantic v2 strtobool behavior — intentional)."""
        with patch.dict("os.environ", {"OPTIMIZE_ON_SHUTDOWN": "on"}):
            s = Settings()
            assert s.optimize_on_shutdown is True

    def test_accepts_off_coerced_to_false(self):
        """'off' is coerced to False (Pydantic v2 strtobool behavior — intentional)."""
        with patch.dict("os.environ", {"OPTIMIZE_ON_SHUTDOWN": "off"}):
            s = Settings()
            assert s.optimize_on_shutdown is False

    def test_rejects_arbitrary_string(self):
        """Arbitrary string 'maybe' should be rejected."""
        with patch.dict("os.environ", {"OPTIMIZE_ON_SHUTDOWN": "maybe"}):
            with pytest.raises(ValidationError):
                Settings()

    def test_rejects_empty_string(self):
        """Empty string should be rejected for bool field."""
        with patch.dict("os.environ", {"OPTIMIZE_ON_SHUTDOWN": ""}):
            with pytest.raises(ValidationError):
                Settings()

    def test_rejects_none_literal(self):
        """None literal should be rejected."""
        with patch.dict("os.environ", {"OPTIMIZE_ON_SHUTDOWN": "None"}):
            with pytest.raises(ValidationError):
                Settings()

    def test_rejects_numeric_float_string(self):
        """Float string '3.14' should be rejected."""
        with patch.dict("os.environ", {"OPTIMIZE_ON_SHUTDOWN": "3.14"}):
            with pytest.raises(ValidationError):
                Settings()

    def test_rejects_path_traversal_string(self):
        """Path traversal string should be rejected."""
        with patch.dict("os.environ", {"OPTIMIZE_ON_SHUTDOWN": "../etc/passwd"}):
            with pytest.raises(ValidationError):
                Settings()

    def test_rejects_template_injection(self):
        """Template injection string should be rejected."""
        with patch.dict("os.environ", {"OPTIMIZE_ON_SHUTDOWN": "${env:USERS_ENABLED}"}):
            with pytest.raises(ValidationError):
                Settings()

    def test_default_is_true(self):
        """Default value should be True when not set."""
        s = Settings()
        assert s.optimize_on_shutdown is True
