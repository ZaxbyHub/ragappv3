"""Multiset-key check for the SAST baseline diff (collated wiki lint
lifecycle trace, issues #929 + #932 + #935).

C11 (AC11, DISCRIMINATING): ``scripts/check_sast_baseline.py``'s
``_diff_added_keys`` keys each baseline finding on
``f"{test_id} {filename}:{line_number}"``, so a PURE line shift (every
finding moving down one line, e.g. an import added above the scanned
region with no content change) is reported as ~113 "Newly-suppressed IDs
(review the swap)" — pure noise that trains reviewers to ignore the swap
list. The check spec-loads the script (importlib.util.spec_from_file_location;
the script's own ``_gate_git`` sibling import resolves file-relative), builds
a ``prev_text`` whose every result has ``line_number + 1`` (a pure shift),
and asserts ``_diff_added_keys(prev_text) == []``. RED at base (113 keys),
GREEN after the fix (multiset-keyed on (test_id, filename, issue_text) with
per-key occurrence counts — issue #935 item 3 names the multiset; plain set
semantics would collapse duplicate content identities, of which the real
baseline carries many).

Anti-over-suppression legs (GREEN at base, must STAY green after the fix):
(a) with the module's ``BASELINE`` pointed at a copy of the real baseline
plus one FABRICATED result entry (new test_id + filename + issue_text), the
fabricated key must still appear in ``_diff_added_keys`` output — so the
fix cannot pass the shift leg by suppressing everything; (b) a DUPLICATE
occurrence of an existing content identity must also be reported, so the
fix cannot pass by switching to collapsing set semantics. The module's
``BASELINE`` is restored between legs so they stay independent.
"""

import importlib.util
import json
import shutil
import unittest
from pathlib import Path
from tempfile import mkdtemp

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "check_sast_baseline.py"
_BASELINE = _REPO_ROOT / "backend" / "security" / "bandit-baseline.json"

_FAB_TEST_ID = "B999"
_FAB_ENTRY = {
    "test_id": _FAB_TEST_ID,
    "filename": "backend/tests/o06e_fabricated_probe.py",
    "line_number": 1,
    "issue_text": (
        "Fabricated o06e probe finding: a real content add must survive "
        "the multiset key"
    ),
}


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "o06e_check_sast_baseline", _SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSastDiffMultisetKey(unittest.TestCase):
    # check: C11 (AC11, DISCRIMINATING) — the shift leg is RED at base
    # (113 keys), GREEN after the fix; the content-add leg is GREEN at
    # base and must stay green.

    def test_line_shift_reports_no_added_keys(self):
        module = _load_module()
        original_text = _BASELINE.read_text(encoding="utf-8")
        original = json.loads(original_text)

        # Anti-over-suppression leg FIRST (green at base, green after the
        # fix): a REAL content add — the current baseline plus one
        # fabricated entry against the unchanged prev text — must still be
        # reported. Point the loaded module's BASELINE at a temp copy so
        # no repository file is touched, and RESTORE it before the next
        # leg so the legs stay independent (plan-critic round 1, blocker 1:
        # the grown baseline leaking into the shift leg contaminated it).
        grown = json.loads(original_text)
        grown.setdefault("results", []).append(_FAB_ENTRY)
        grown_dir = Path(mkdtemp())
        self.addCleanup(shutil.rmtree, grown_dir, True)
        grown_path = grown_dir / "o06e-grown-baseline.json"
        grown_path.write_text(json.dumps(grown), encoding="utf-8")
        module.BASELINE = grown_path
        added_real = module._diff_added_keys(original_text)
        self.assertTrue(
            any(_FAB_TEST_ID in key for key in added_real),
            f"a real content add must still be reported by _diff_added_keys; "
            f"got {added_real!r}",
        )
        # Multiset leg (plan-critic round 1, blocker 2): a duplicate
        # occurrence of an EXISTING (test_id, filename, issue_text) identity
        # is a real content add too — set semantics would collapse it. The
        # duplicate carries a shifted line_number so this leg is GREEN at
        # base (the line-keyed diff sees a new key) and must STAY green
        # after the content-multiset fix (same identity, +1 occurrence);
        # only a set-semantics implementation goes red here.
        dup_source = next(
            r for r in original.get("results", []) if r.get("test_id")
        )
        dup = dict(dup_source)
        dup["line_number"] = int(dup_source.get("line_number", 1)) + 1000
        grown_dup = json.loads(original_text)
        grown_dup.setdefault("results", []).append(dup)
        dup_dir = Path(mkdtemp())
        self.addCleanup(shutil.rmtree, dup_dir, True)
        dup_path = dup_dir / "o06e-dup-baseline.json"
        dup_path.write_text(json.dumps(grown_dup), encoding="utf-8")
        module.BASELINE = dup_path
        added_dup = module._diff_added_keys(original_text)
        self.assertTrue(
            added_dup,
            "a duplicate occurrence of an existing content identity must "
            "still be reported (multiset, not set, semantics); got []",
        )
        # Restore the real baseline before the shift leg.
        module.BASELINE = _BASELINE

        # Discriminating leg: a PURE line shift (every result's
        # line_number +1, content otherwise identical) must report NO
        # added keys — the finding identities did not change. RED at base.
        shifted = json.loads(original_text)
        for result in shifted.get("results", []):
            if isinstance(result.get("line_number"), int):
                result["line_number"] = result["line_number"] + 1
        added_shift = module._diff_added_keys(json.dumps(shifted))
        self.assertEqual(
            len(added_shift), 0,
            f"a pure line shift reported {len(added_shift)} added keys; "
            f"first 3: {added_shift[:3]!r}",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
