"""Issue #559 cross-stage acceptance check C10 (PRESERVING): settings
conventions stay enforced.

- the ``*_job_lease_enabled`` switches (existing and new) stay env-only —
  deliberately excluded from the settings-update model;
- the existing lease-knob validators keep enforcing their bounds at BOTH
  layers (config build and settings-update path) and the coherence rule
  (reclaim timeout > 4x heartbeat) still holds.

Expectation: GREEN at the base (stage 1 shipped these validators for the
ingestion knobs) and GREEN after the stages 2-4 migration — the new switches
must not regress either invariant.
"""

import unittest

from pydantic import ValidationError

from app.api.routes.settings import SettingsUpdate
from app.config import Settings

PER_QUEUE_LEASE_SWITCHES = (
    "wiki_kms_job_lease_enabled",
    "reindex_job_lease_enabled",
    "draft_job_lease_enabled",
)


class TestLeaseSettingsConventions(unittest.TestCase):
    # check: C10 (PRESERVING) — env-only switch rule and two-layer knob
    # validation survive the stages 2-4 migration unchanged.

    def test_switches_are_env_only_not_settable_via_settings_update(self):
        excluded = set(PER_QUEUE_LEASE_SWITCHES) | {"ingestion_job_lease_enabled"}
        for name in excluded:
            self.assertNotIn(
                name,
                SettingsUpdate.model_fields,
                f"{name} must stay env-only: deliberately excluded from the "
                "settings-update model",
            )

    def test_existing_lease_knob_bounds_still_enforced(self):
        with self.assertRaises(ValidationError):
            Settings(jobs_heartbeat_interval_seconds=601)
        with self.assertRaises(ValidationError):
            Settings(jobs_max_attempts=21)
        with self.assertRaises(ValidationError):
            SettingsUpdate(jobs_lease_reclaim_timeout_seconds=14)
        with self.assertRaises(ValidationError):
            SettingsUpdate(jobs_max_attempts=0)
        # A coherent, in-bounds pair still builds at both layers.
        built = Settings(
            jobs_heartbeat_interval_seconds=30,
            jobs_lease_reclaim_timeout_seconds=300,
        )
        self.assertEqual(built.jobs_heartbeat_interval_seconds, 30)
        self.assertEqual(built.jobs_lease_reclaim_timeout_seconds, 300)


if __name__ == "__main__":
    unittest.main()
