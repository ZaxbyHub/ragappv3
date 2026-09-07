"""Settings-leak guardrail self-tests (issue #462 B3 / TEST-004 defect class).

The repo-wide rung is the autouse ``_guard_multimodal_vision_flag`` fixture in
``tests/conftest.py``. These self-tests exercise the REAL detection predicate
the fixture uses (``conftest.multimodal_vision_flag_leaked``, imported here) so
a regression in the guard's detection logic fails here — plus a source-
inspection pin that the fixture stays autouse and wired (PRR-002/PRR-003,
PR #525 review).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import conftest as conftest_module  # noqa: E402  (repo precedent: test_agentic_control_parity)
from app.config import settings  # noqa: E402

FLAG = "multimodal_query_vision_enabled"


def test_detector_flags_unrestored_mutation():
    """The REAL predicate the conftest fixture uses must report a leak for an
    un-restored mutation (before != after)."""
    incoming = getattr(settings, FLAG)
    try:
        setattr(settings, FLAG, not incoming)
        after = getattr(settings, FLAG)
        assert conftest_module.multimodal_vision_flag_leaked(incoming, after) is True
    finally:
        setattr(settings, FLAG, incoming)


def test_detector_silent_on_scoped_patch():
    """A scoped patch (patch.object) restores the incoming value, so the REAL
    predicate must report NO leak when after == before."""
    incoming = getattr(settings, FLAG)
    setattr(settings, FLAG, incoming)  # simulate the patch.object restore path
    after = getattr(settings, FLAG)
    assert conftest_module.multimodal_vision_flag_leaked(incoming, after) is False


def test_conftest_guard_fixture_is_wired():
    """Source-inspection pin (PRR-002): the autouse decorator must be attached
    to the guard fixture ITSELF (decorator/name adjacency), not merely present
    anywhere in conftest.py — otherwise demoting the guard to non-autouse while
    other autouse fixtures exist would pass this pin."""
    conftest_path = os.path.join(os.path.dirname(__file__), "conftest.py")
    with open(conftest_path, "r", encoding="utf-8") as fh:
        source = fh.read()
    assert (
        "@pytest.fixture(autouse=True)\ndef _guard_multimodal_vision_flag()" in source
    ), "the _guard_multimodal_vision_flag fixture must be autouse"
    assert "def multimodal_vision_flag_leaked(before, after)" in source
    assert FLAG in source
    # The fixture must consult the shared predicate, not a private comparison.
    assert "multimodal_vision_flag_leaked(before, after)" in source
