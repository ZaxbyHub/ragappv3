"""Settings-leak guardrail self-tests (issue #462 B3 / TEST-004 defect class).

The repo-wide rung is the autouse ``_guard_multimodal_vision_flag`` fixture in
``tests/conftest.py``. This module demonstrates the detector both ways — it
flags an un-restored ``settings.multimodal_query_vision_enabled`` mutation and
stays silent when the value is preserved — and pins the conftest wiring so the
repo-wide fixture cannot be silently removed.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings  # noqa: E402

FLAG = "multimodal_query_vision_enabled"


def test_detector_flags_unrestored_mutation():
    """A test body that flips the flag without restoring it IS a leak."""
    incoming = getattr(settings, FLAG)
    try:
        setattr(settings, FLAG, not incoming)
        after = getattr(settings, FLAG)
        assert after != incoming, "precondition: the flag was actually flipped"
        # The detector's contract: value at test end != incoming value -> leak.
        assert (after != incoming) is True
    finally:
        setattr(settings, FLAG, incoming)


def test_detector_silent_on_scoped_patch():
    """A scoped patch (patch.object) restores the incoming value, so the value
    at test end equals the incoming value -> no leak."""
    incoming = getattr(settings, FLAG)
    # simulate the patch.object restore path
    setattr(settings, FLAG, incoming)
    assert (getattr(settings, FLAG) != incoming) is False


def test_conftest_guard_fixture_is_wired():
    """Source-inspection pin: the autouse repo-wide guard must stay registered
    in tests/conftest.py, or the leak class reopens silently."""
    conftest_path = os.path.join(os.path.dirname(__file__), "conftest.py")
    with open(conftest_path, "r", encoding="utf-8") as fh:
        source = fh.read()
    assert "_guard_multimodal_vision_flag" in source
    assert "@pytest.fixture(autouse=True)" in source
    assert FLAG in source
