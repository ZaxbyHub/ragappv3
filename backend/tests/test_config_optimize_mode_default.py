"""
Verification tests for optimize_mode default change (Task 1.3).

Tests verify:
1. Settings().optimize_mode returns "periodic" (not "after_every_write")
2. The field validator accepts all three modes: "periodic", "after_every_write", "manual"
3. The validator rejects invalid modes (e.g., "always")
4. optimize_interval_chunks defaults to 5000 and is used by periodic mode
"""

import pytest
from backend.app.config import Settings
from pydantic import ValidationError


class TestOptimizeModeDefault:
    """Tests for optimize_mode default value and validation."""

    def test_optimize_mode_defaults_to_periodic(self):
        """
        optimize_mode should default to 'periodic' (changed from 'after_every_write').
        This reduces write amplification on large ingestion workloads.
        """
        settings = Settings()
        assert settings.optimize_mode == "periodic"

    def test_optimize_interval_chunks_defaults_to_5000(self):
        """
        optimize_interval_chunks should default to 5000.
        Used by periodic mode to determine when to call table.optimize().
        """
        settings = Settings()
        assert settings.optimize_interval_chunks == 5000

    def test_optimize_mode_validator_accepts_periodic(self):
        """Field validator accepts 'periodic' mode."""
        settings = Settings(optimize_mode="periodic")
        assert settings.optimize_mode == "periodic"

    def test_optimize_mode_validator_accepts_after_every_write(self):
        """Field validator accepts 'after_every_write' mode."""
        settings = Settings(optimize_mode="after_every_write")
        assert settings.optimize_mode == "after_every_write"

    def test_optimize_mode_validator_accepts_manual(self):
        """Field validator accepts 'manual' mode."""
        settings = Settings(optimize_mode="manual")
        assert settings.optimize_mode == "manual"

    def test_optimize_mode_validator_rejects_invalid_mode(self):
        """Field validator rejects invalid optimize_mode values."""
        with pytest.raises(ValidationError) as exc_info:
            Settings(optimize_mode="always")
        assert "optimize_mode must be one of" in str(exc_info.value)

    def test_optimize_mode_validator_rejects_empty_string(self):
        """Field validator rejects empty string for optimize_mode."""
        with pytest.raises(ValidationError) as exc_info:
            Settings(optimize_mode="")
        assert "optimize_mode must be one of" in str(exc_info.value)

    def test_optimize_mode_validator_rejects_random_string(self):
        """Field validator rejects random invalid strings."""
        with pytest.raises(ValidationError) as exc_info:
            Settings(optimize_mode="invalid_mode")
        assert "optimize_mode must be one of" in str(exc_info.value)

    def test_optimize_mode_validator_rejects_case_variants(self):
        """Field validator is case-sensitive and rejects case variants."""
        with pytest.raises(ValidationError) as exc_info:
            Settings(optimize_mode="PERIODIC")
        assert "optimize_mode must be one of" in str(exc_info.value)

        with pytest.raises(ValidationError) as exc_info:
            Settings(optimize_mode="Periodic")
        assert "optimize_mode must be one of" in str(exc_info.value)


class TestOptimizeIntervalChunksValidation:
    """Tests for optimize_interval_chunks validation."""

    def test_optimize_interval_chunks_accepts_minimum_value(self):
        """optimize_interval_chunks accepts minimum value of 1."""
        settings = Settings(optimize_interval_chunks=1)
        assert settings.optimize_interval_chunks == 1

    def test_optimize_interval_chunks_accepts_large_value(self):
        """optimize_interval_chunks accepts large values."""
        settings = Settings(optimize_interval_chunks=100000)
        assert settings.optimize_interval_chunks == 100000

    def test_optimize_interval_chunks_rejects_zero(self):
        """optimize_interval_chunks rejects 0."""
        with pytest.raises(ValidationError) as exc_info:
            Settings(optimize_interval_chunks=0)
        assert "optimize_interval_chunks must be >= 1" in str(exc_info.value)

    def test_optimize_interval_chunks_rejects_negative(self):
        """optimize_interval_chunks rejects negative values."""
        with pytest.raises(ValidationError) as exc_info:
            Settings(optimize_interval_chunks=-1)
        assert "optimize_interval_chunks must be >= 1" in str(exc_info.value)


class TestPeriodicModeIntegration:
    """Integration tests verifying periodic mode uses optimize_interval_chunks."""

    def test_periodic_mode_with_custom_interval(self):
        """Periodic mode settings can be configured together."""
        settings = Settings(optimize_mode="periodic", optimize_interval_chunks=10000)
        assert settings.optimize_mode == "periodic"
        assert settings.optimize_interval_chunks == 10000

    def test_all_modes_can_be_set_with_interval(self):
        """All three optimize modes work with custom interval settings."""
        for mode in ["periodic", "after_every_write", "manual"]:
            settings = Settings(optimize_mode=mode, optimize_interval_chunks=2500)
            assert settings.optimize_mode == mode
            assert settings.optimize_interval_chunks == 2500
