"""Issue #559 MINOR-3 unit test (binding plan revision): job-lease settings
validation at BOTH layers.

Layer 1 — config.py env/startup validation: range validators on
``jobs_heartbeat_interval_seconds`` / ``jobs_lease_reclaim_timeout_seconds``
/ ``jobs_max_attempts`` plus the cross-field coherence model validator
(reclaim timeout must exceed 4x the heartbeat interval).

Layer 2 — the settings SAVE path: ``SettingsUpdate`` persists via bare
setattr onto the settings singleton, which bypasses Settings model
validators — so the PUT path carries its own effective-state
(body-value-else-current) coherence check. An update that would leave the
running process with ``reclaim <= 4x heartbeat`` must be rejected (the
R4-S16 janitor-steals-live-lease hazard).
"""

import pytest
from pydantic import ValidationError

from app.api.routes.settings import SettingsUpdate
from app.config import Settings, settings


class TestConfigLayerValidation:
    """Layer 1: env/startup validators on the Settings model itself."""

    def _build(self, **overrides) -> Settings:
        """Build a throwaway Settings with the conftest env still applied.

        The module-level singleton is untouched; constructing a second
        instance runs the full validator chain.
        """
        return Settings(**overrides)

    def test_valid_lease_pair_accepted(self):
        built = self._build(
            jobs_heartbeat_interval_seconds=30,
            jobs_lease_reclaim_timeout_seconds=300,
        )
        assert built.jobs_heartbeat_interval_seconds == 30
        assert built.jobs_lease_reclaim_timeout_seconds == 300

    def test_incoherent_lease_pair_rejected(self):
        with pytest.raises(ValidationError, match="4x"):
            self._build(
                jobs_heartbeat_interval_seconds=500,
                jobs_lease_reclaim_timeout_seconds=1000,
            )

    def test_boundary_exactly_4x_rejected(self):
        with pytest.raises(ValidationError, match="4x"):
            self._build(
                jobs_heartbeat_interval_seconds=50,
                jobs_lease_reclaim_timeout_seconds=200,
            )

    def test_zero_heartbeat_rejected(self):
        with pytest.raises(ValidationError):
            self._build(jobs_heartbeat_interval_seconds=0)

    def test_negative_heartbeat_rejected(self):
        with pytest.raises(ValidationError):
            self._build(jobs_heartbeat_interval_seconds=-5)

    def test_zero_reclaim_timeout_rejected(self):
        with pytest.raises(ValidationError):
            self._build(jobs_lease_reclaim_timeout_seconds=0)

    def test_heartbeat_above_range_rejected(self):
        with pytest.raises(ValidationError):
            self._build(jobs_heartbeat_interval_seconds=601)

    def test_reclaim_timeout_below_range_rejected(self):
        with pytest.raises(ValidationError):
            self._build(jobs_lease_reclaim_timeout_seconds=14)

    def test_max_attempts_zero_rejected(self):
        with pytest.raises(ValidationError):
            self._build(jobs_max_attempts=0)

    def test_max_attempts_above_range_rejected(self):
        with pytest.raises(ValidationError):
            self._build(jobs_max_attempts=21)


class TestSettingsUpdateLayerValidation:
    """Layer 2: the PUT path's effective-state coherence check."""

    def test_incoherent_body_pair_rejected(self):
        with pytest.raises(ValidationError, match="4x"):
            SettingsUpdate(
                jobs_heartbeat_interval_seconds=500,
                jobs_lease_reclaim_timeout_seconds=1000,
            )

    def test_heartbeat_update_measured_against_current_reclaim_rejected(self):
        # Body sets ONLY the heartbeat: effective pair is (100, current 300)
        # -> 300 <= 4x100, which would leave the live process incoherent.
        with pytest.raises(ValidationError, match="4x"):
            SettingsUpdate(jobs_heartbeat_interval_seconds=100)

    def test_reclaim_update_measured_against_current_heartbeat_accepted(self):
        # Effective pair (current 30, 1000): 1000 > 120 — coherent.
        update = SettingsUpdate(jobs_lease_reclaim_timeout_seconds=1000)
        assert update.jobs_lease_reclaim_timeout_seconds == 1000

    def test_omitted_fields_fall_back_to_current_singleton(self):
        # Sanity for the body-value-else-current semantics the coherence
        # check relies on: with nothing set, no check can fire.
        update = SettingsUpdate()
        assert update.jobs_heartbeat_interval_seconds is None
        assert update.jobs_lease_reclaim_timeout_seconds is None
        assert settings.jobs_lease_reclaim_timeout_seconds > (
            4 * settings.jobs_heartbeat_interval_seconds
        )

    def test_max_attempts_zero_rejected_on_update(self):
        with pytest.raises(ValidationError):
            SettingsUpdate(jobs_max_attempts=0)

    def test_max_attempts_above_range_rejected_on_update(self):
        with pytest.raises(ValidationError):
            SettingsUpdate(jobs_max_attempts=21)
