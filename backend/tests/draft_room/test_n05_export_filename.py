"""Arm's-length acceptance checks for issue #789 AC7 (export filenames).

``_export_filename`` feeds a *content title* to ``secure_filename``, whose
first step is ``os.path.basename`` -- so a title containing a path separator
silently loses everything before it (``A/B test results`` exports as
``B_test_results-rev1-REVIEW.md``) and non-ASCII text is dropped entirely.
The frozen expectations here: keep as much of the title as possible while
staying filesystem-safe.

``test_export_filename_keeps_text_before_a_slash`` is DISCRIMINATING -- RED
at base, GREEN after the fix. The backslash/CJK companion is RED at base for
the backslash case on Windows (where ``os.path.basename`` treats ``\\`` as a
separator) and pins safety properties that hold at both ends otherwise.

Plain unit file: no HTTP, no fixture. The import of the route module relies
on the test-conf environment only insofar as any module import does.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.api.routes.draft_room import _export_filename  # noqa: E402


def test_export_filename_keeps_text_before_a_slash():
    """AC7. A slash in the title must not discard everything before it:
    the exported name keeps the ``A`` prefix and the ``B_test_results``
    remainder, with no path separator anywhere. RED at base (the name is
    ``B_test_results-rev1-REVIEW.md``), GREEN after the fix."""
    name = _export_filename("A/B test results", 1, "REVIEW")

    assert name[0] == "A", f"text before the slash was dropped: {name!r}"
    assert "B_test_results" in name, name
    assert "/" not in name, name
    assert "\\" not in name, name
    assert name.endswith(".md"), name


def test_export_filename_backslash_and_nonascii_stay_safe():
    """Companion safety checks. The backslash title keeps its text
    before the separator and no separator survives (RED at base on
    Windows, where ``os.path.basename`` eats ``Q3\\``; GREEN after the
    fix). A CJK-only title must yield a non-empty, separator-free,
    ``.md``-suffixed name -- the fallback path is acceptable when nothing
    ASCII survives, so this part holds at both ends."""
    backslash = _export_filename("Q3\\Q4 plan", 2, "REVIEW")
    assert backslash.startswith("Q3"), (
        f"text before the backslash was dropped: {backslash!r}"
    )
    assert "\\" not in backslash, backslash
    assert "/" not in backslash, backslash
    assert backslash.endswith(".md"), backslash

    cjk = _export_filename("季度报告", 3, "REVIEW")
    assert cjk, "a CJK-only title must still produce a filename"
    assert "/" not in cjk, cjk
    assert "\\" not in cjk, cjk
    assert cjk.endswith(".md"), cjk
