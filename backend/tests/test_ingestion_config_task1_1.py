"""
Verification tests for Task 1.1 new ingestion performance config fields.

Tests the 5 new fields added to backend/app/config.py:
1. ingestion_worker_count: int = 2  (range 1-16)
2. optimize_mode: str = "periodic"   (after_every_write/periodic/manual)
3. optimize_interval_chunks: int = 5000  (>= 1)
4. embedding_concurrent_batches: int = 4  (range 1-16)
5. optimize_on_shutdown: bool = True  (no validator)
"""

import os
import sys
from unittest.mock import patch

import pytest
from pydantic import ValidationError

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import Settings


class TestNewIngestionFieldsDefaults:
    """Test that new ingestion fields have correct defaults."""

    def test_ingestion_worker_count_default(self):
        """ingestion_worker_count should default to 2."""
        settings = Settings()
        assert settings.ingestion_worker_count == 2

    def test_optimize_mode_default(self):
        """optimize_mode should default to 'periodic'."""
        settings = Settings()
        assert settings.optimize_mode == "periodic"

    def test_optimize_interval_chunks_default(self):
        """optimize_interval_chunks should default to 5000."""
        settings = Settings()
        assert settings.optimize_interval_chunks == 5000

    def test_embedding_concurrent_batches_default(self):
        """embedding_concurrent_batches should default to 4."""
        settings = Settings()
        assert settings.embedding_concurrent_batches == 4

    def test_optimize_on_shutdown_default(self):
        """optimize_on_shutdown should default to True."""
        settings = Settings()
        assert settings.optimize_on_shutdown is True


class TestIngestionWorkerCountValidator:
    """Test ingestion_worker_count range validator (1-16)."""

    def test_valid_values(self):
        """Accept any integer 1-16."""
        for v in [1, 2, 8, 16]:
            with patch.dict("os.environ", {"INGESTION_WORKER_COUNT": str(v)}):
                settings = Settings()
                assert settings.ingestion_worker_count == v

    def test_boundary_min(self):
        """Minimum boundary value 1 is accepted."""
        with patch.dict("os.environ", {"INGESTION_WORKER_COUNT": "1"}):
            settings = Settings()
            assert settings.ingestion_worker_count == 1

    def test_boundary_max(self):
        """Maximum boundary value 16 is accepted."""
        with patch.dict("os.environ", {"INGESTION_WORKER_COUNT": "16"}):
            settings = Settings()
            assert settings.ingestion_worker_count == 16

    def test_out_of_range_below_min(self):
        """Value 0 is rejected."""
        with patch.dict("os.environ", {"INGESTION_WORKER_COUNT": "0"}):
            with pytest.raises(ValidationError) as exc_info:
                Settings()
            assert "ingestion_worker_count must be >= 1" in str(exc_info.value)

    def test_out_of_range_above_max(self):
        """Value 17 is rejected."""
        with patch.dict("os.environ", {"INGESTION_WORKER_COUNT": "17"}):
            with pytest.raises(ValidationError) as exc_info:
                Settings()
            assert "ingestion_worker_count must be <= 16" in str(exc_info.value)


class TestOptimizeModeValidator:
    """Test optimize_mode enum validator."""

    def test_valid_values(self):
        """Accept all three valid modes."""
        for mode in ["after_every_write", "periodic", "manual"]:
            with patch.dict("os.environ", {"OPTIMIZE_MODE": mode}):
                settings = Settings()
                assert settings.optimize_mode == mode

    def test_invalid_value(self):
        """Invalid mode is rejected."""
        with patch.dict("os.environ", {"OPTIMIZE_MODE": "invalid_mode"}):
            with pytest.raises(ValidationError) as exc_info:
                Settings()
            assert "optimize_mode must be one of" in str(exc_info.value)


class TestOptimizeIntervalChunksValidator:
    """Test optimize_interval_chunks >= 1 validator."""

    def test_valid_values(self):
        """Accept positive integers."""
        for v in [1, 2, 100, 5000, 100000]:
            with patch.dict("os.environ", {"OPTIMIZE_INTERVAL_CHUNKS": str(v)}):
                settings = Settings()
                assert settings.optimize_interval_chunks == v

    def test_boundary_min(self):
        """Minimum boundary value 1 is accepted."""
        with patch.dict("os.environ", {"OPTIMIZE_INTERVAL_CHUNKS": "1"}):
            settings = Settings()
            assert settings.optimize_interval_chunks == 1

    def test_out_of_range_zero(self):
        """Value 0 is rejected."""
        with patch.dict("os.environ", {"OPTIMIZE_INTERVAL_CHUNKS": "0"}):
            with pytest.raises(ValidationError) as exc_info:
                Settings()
            assert "optimize_interval_chunks must be >= 1" in str(exc_info.value)

    def test_out_of_range_negative(self):
        """Negative values are rejected."""
        with patch.dict("os.environ", {"OPTIMIZE_INTERVAL_CHUNKS": "-1"}):
            with pytest.raises(ValidationError) as exc_info:
                Settings()
            assert "optimize_interval_chunks must be >= 1" in str(exc_info.value)


class TestEmbeddingConcurrentBatchesValidator:
    """Test embedding_concurrent_batches range validator (1-16)."""

    def test_valid_values(self):
        """Accept any integer 1-16."""
        for v in [1, 2, 4, 8, 16]:
            with patch.dict("os.environ", {"EMBEDDING_CONCURRENT_BATCHES": str(v)}):
                settings = Settings()
                assert settings.embedding_concurrent_batches == v

    def test_boundary_min(self):
        """Minimum boundary value 1 is accepted."""
        with patch.dict("os.environ", {"EMBEDDING_CONCURRENT_BATCHES": "1"}):
            settings = Settings()
            assert settings.embedding_concurrent_batches == 1

    def test_boundary_max(self):
        """Maximum boundary value 16 is accepted."""
        with patch.dict("os.environ", {"EMBEDDING_CONCURRENT_BATCHES": "16"}):
            settings = Settings()
            assert settings.embedding_concurrent_batches == 16

    def test_out_of_range_below_min(self):
        """Value 0 is rejected."""
        with patch.dict("os.environ", {"EMBEDDING_CONCURRENT_BATCHES": "0"}):
            with pytest.raises(ValidationError) as exc_info:
                Settings()
            assert "embedding_concurrent_batches must be >= 1" in str(exc_info.value)

    def test_out_of_range_above_max(self):
        """Value 17 is rejected."""
        with patch.dict("os.environ", {"EMBEDDING_CONCURRENT_BATCHES": "17"}):
            with pytest.raises(ValidationError) as exc_info:
                Settings()
            assert "embedding_concurrent_batches must be <= 16" in str(exc_info.value)


class TestOptimizeOnShutdownBool:
    """Test optimize_on_shutdown is a simple bool with no validator."""

    def test_default_is_true(self):
        """optimize_on_shutdown defaults to True."""
        settings = Settings()
        assert settings.optimize_on_shutdown is True

    def test_can_be_set_to_false(self):
        """optimize_on_shutdown can be set to False."""
        with patch.dict("os.environ", {"OPTIMIZE_ON_SHUTDOWN": "false"}):
            settings = Settings()
            assert settings.optimize_on_shutdown is False

    def test_can_be_set_to_true(self):
        """optimize_on_shutdown can be explicitly set to True."""
        with patch.dict("os.environ", {"OPTIMIZE_ON_SHUTDOWN": "true"}):
            settings = Settings()
            assert settings.optimize_on_shutdown is True


class TestNewFieldsEnvOverride:
    """Test that all 5 new fields can be overridden via environment variables."""

    def test_ingestion_worker_count_env_override(self):
        """ingestion_worker_count can be overridden via INGESTION_WORKER_COUNT env var."""
        with patch.dict("os.environ", {"INGESTION_WORKER_COUNT": "8"}):
            settings = Settings()
            assert settings.ingestion_worker_count == 8

    def test_optimize_mode_env_override(self):
        """optimize_mode can be overridden via OPTIMIZE_MODE env var."""
        with patch.dict("os.environ", {"OPTIMIZE_MODE": "after_every_write"}):
            settings = Settings()
            assert settings.optimize_mode == "after_every_write"

    def test_optimize_interval_chunks_env_override(self):
        """optimize_interval_chunks can be overridden via OPTIMIZE_INTERVAL_CHUNKS env var."""
        with patch.dict("os.environ", {"OPTIMIZE_INTERVAL_CHUNKS": "10000"}):
            settings = Settings()
            assert settings.optimize_interval_chunks == 10000

    def test_embedding_concurrent_batches_env_override(self):
        """embedding_concurrent_batches can be overridden via EMBEDDING_CONCURRENT_BATCHES env var."""
        with patch.dict("os.environ", {"EMBEDDING_CONCURRENT_BATCHES": "12"}):
            settings = Settings()
            assert settings.embedding_concurrent_batches == 12

    def test_optimize_on_shutdown_env_override(self):
        """optimize_on_shutdown can be overridden via OPTIMIZE_ON_SHUTDOWN env var."""
        with patch.dict("os.environ", {"OPTIMIZE_ON_SHUTDOWN": "False"}):
            settings = Settings()
            assert settings.optimize_on_shutdown is False


class TestAllFiveFieldsTogether:
    """Test all 5 new fields can coexist in a single Settings instance."""

    def test_all_defaults_together(self):
        """All 5 fields have correct defaults simultaneously."""
        settings = Settings()
        assert settings.ingestion_worker_count == 2
        assert settings.optimize_mode == "periodic"
        assert settings.optimize_interval_chunks == 5000
        assert settings.embedding_concurrent_batches == 4
        assert settings.optimize_on_shutdown is True

    def test_all_custom_together(self):
        """All 5 fields can be set together without conflict."""
        with patch.dict(
            "os.environ",
            {
                "INGESTION_WORKER_COUNT": "10",
                "OPTIMIZE_MODE": "manual",
                "OPTIMIZE_INTERVAL_CHUNKS": "2500",
                "EMBEDDING_CONCURRENT_BATCHES": "8",
                "OPTIMIZE_ON_SHUTDOWN": "false",
            },
        ):
            settings = Settings()
            assert settings.ingestion_worker_count == 10
            assert settings.optimize_mode == "manual"
            assert settings.optimize_interval_chunks == 2500
            assert settings.embedding_concurrent_batches == 8
            assert settings.optimize_on_shutdown is False
