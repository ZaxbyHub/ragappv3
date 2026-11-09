"""Arm's-length acceptance check C10 for the draft-capability-liveness trace
(issues #902/#905/#911/#913), startup reconcile side.

Frozen specification. DISCRIMINATING: RED at base, GREEN after the fix.

* C10 -- ``DraftInputStorage.reconcile(valid_pairs, max_age_seconds=...,
  valid_input_relpaths={...})`` must remove a row-less finalized file that
  sits inside a LIVE (owner, draft) project directory (its relpath not in
  ``valid_input_relpaths``) and report it in the returned counts under a
  NEW key ``orphan_files_removed`` (>= 1). A file whose relpath IS in the
  valid set is untouched, and with the legacy call shape
  (``valid_input_relpaths`` omitted/None) nothing inside live project
  directories is removed. At base reconcile only removes whole ORPHAN
  project directories; files inside live project dirs are never touched
  and the ``orphan_files_removed`` key does not exist.

  Safety/negative legs (must not over-delete once the exact-rule removal
  ships): case handling follows the FILESYSTEM (platform-branched): the
  exact-case valid file is always kept; on a case-SENSITIVE filesystem
  (POSIX — CI's default) a case-differing file IS an orphan candidate and
  IS removed, while on a case-INSENSITIVE filesystem (Windows) the
  case-differing file IS the valid row's file and is NOT removed (normcase
  folds); a REAL file whose valid relpath entry uses backslash separators
  is NOT removed (separators are normalized before comparing); files
  OUTSIDE the canonical ``<owner>/<draft>/inputs/<input_id>/<name>``
  layout (directly under the draft dir, or nested one level deeper) are
  NOT removed; and a symlink/junction inside inputs/ pointing outside the
  storage root is never FOLLOWED — the sweep must not delete through it
  and the target's bytes stay untouched (skipped when the platform cannot
  create one; on Windows a junction via ``cmd /c mklink /J`` needs no
  privilege).
"""

import os
import pathlib
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

from tests.draft_room.test_draft_input_storage import DraftInputStorageTestBase

LIVE_PAIR = (7, 3)


class TestN05EOrphanInputReconcile(DraftInputStorageTestBase):
    """C10 (DISCRIMINATING)."""

    def _write_finalized_file(self, relpath: str, content: bytes = b"orphaned") -> str:
        path = self.root
        for part in relpath.split("/"):
            path = path / part
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return relpath

    def test_row_less_file_in_live_project_dir_removed_and_counted(self):
        """RED at base, GREEN after the fix: the pair (7, 3) is live, but a
        finalized file inside its project dir with no backing draft_inputs
        row is stale bytes and must be removed and counted under
        ``orphan_files_removed``."""
        keep = self._write_finalized_file("7/3/inputs/10/keep.txt", b"still owed")
        orphan = self._write_finalized_file("7/3/inputs/11/orphan.txt", b"row-less")

        counts = self.storage.reconcile(
            {LIVE_PAIR}, max_age_seconds=3600, valid_input_relpaths={keep}
        )

        removed = counts.get("orphan_files_removed", 0)
        self.assertGreaterEqual(
            removed,
            1,
            "reconcile must report the removed row-less finalized file under "
            f"the new orphan_files_removed key, got counts {counts}",
        )
        self.assertFalse(
            self.storage.exists(orphan),
            "a finalized file whose relpath is not in valid_input_relpaths "
            "must be removed from the live project directory",
        )
        self.assertTrue(
            self.storage.exists(keep),
            "a finalized file whose relpath IS in valid_input_relpaths must "
            "be untouched",
        )

    def test_legacy_call_shape_removes_nothing_inside_live_project_dirs(self):
        """Guardrail for the legacy startup call shape: with
        ``valid_input_relpaths`` omitted, reconcile must not remove files
        inside live project directories (it cannot know which bytes are
        still owed, so the exact-rule removal must stay opt-in)."""
        first = self._write_finalized_file("7/3/inputs/10/keep.txt", b"still owed")
        second = self._write_finalized_file("7/3/inputs/11/orphan.txt", b"row-less")

        counts = self.storage.reconcile({LIVE_PAIR}, max_age_seconds=3600)

        self.assertEqual(
            counts.get("orphan_files_removed", 0),
            0,
            "the legacy pairs-only call shape must not remove files inside "
            f"live project directories, got counts {counts}",
        )
        self.assertTrue(self.storage.exists(first))
        self.assertTrue(self.storage.exists(second))

    # -- safety legs: the exact-rule sweep must not over-delete ---------------

    def _filesystem_is_case_sensitive(self):
        """Probe the filesystem backing temp storage: on a case-sensitive
        (POSIX-style) filesystem two names differing only by case are
        DISTINCT files; on a case-insensitive one they alias the SAME file
        (writing A.txt overwrites a.txt — detect by content, since the
        aliased open does not raise)."""
        import shutil
        import tempfile

        probe = pathlib.Path(tempfile.mkdtemp())
        try:
            (probe / "a.txt").write_bytes(b"one")
            (probe / "A.txt").write_bytes(b"two")
            return (probe / "a.txt").read_bytes() == b"one"
        finally:
            shutil.rmtree(probe, ignore_errors=True)

    def test_case_mismatch_handling_follows_the_filesystem(self):
        """Platform-branched contract (``os.path.normcase`` semantics). The
        exact-case valid file is kept on every platform. On a CASE-SENSITIVE
        filesystem (POSIX — CI's default), normcase is the identity, so a
        file whose case differs from every valid relpath IS an orphan
        candidate and IS removed (DISCRIMINATING there). On a
        CASE-INSENSITIVE filesystem (this Windows box), normcase folds, so
        the case-differing file IS the valid row's file and must NOT be
        removed (safety)."""
        keep = self._write_finalized_file(
            "7/3/inputs/10/keep.txt", b"owed by a row (exact case)"
        )
        variant = self._write_finalized_file(
            "7/3/inputs/11/orphan.txt", b"case differs from every valid entry"
        )

        counts = self.storage.reconcile(
            {LIVE_PAIR},
            max_age_seconds=3600,
            valid_input_relpaths={keep, "7/3/inputs/11/ORPHAN.TXT"},
        )

        self.assertTrue(
            self.storage.exists(keep),
            "the exact-case valid relpath's file must be kept on every "
            f"platform (counts {counts})",
        )
        if self._filesystem_is_case_sensitive():
            self.assertFalse(
                self.storage.exists(variant),
                "on a case-SENSITIVE filesystem a file whose case differs "
                "from every valid relpath is an orphan candidate and must be "
                f"removed (counts {counts})",
            )
        else:
            self.assertTrue(
                self.storage.exists(variant),
                "on a case-INSENSITIVE filesystem the case-differing file IS "
                "the valid row's file (normcase folds) and must not be "
                f"removed (counts {counts})",
            )

    def test_separator_normalized_valid_relpath_is_not_removed(self):
        """GREEN at base, must stay green after the fix: a REAL ``other.txt``
        whose valid relpath entry uses backslash separators must be treated
        as owed — the implementation normalizes separators when comparing,
        so the on-disk canonical form must not be reaped."""
        keep = self._write_finalized_file("7/3/inputs/10/keep.txt", b"owed by a row")
        other = self._write_finalized_file(
            "7/3/inputs/12/other.txt", b"valid entry written with backslashes"
        )

        counts = self.storage.reconcile(
            {LIVE_PAIR},
            max_age_seconds=3600,
            valid_input_relpaths={keep, "7\\3\\inputs\\12\\other.txt"},
        )

        self.assertTrue(
            self.storage.exists(other),
            "a file whose valid relpath entry uses backslash separators must "
            f"not be removed — separators are normalized before comparing "
            f"(counts {counts})",
        )

    def test_files_outside_the_canonical_layout_are_not_removed(self):
        """GREEN at base, must stay green after the fix: only files at
        ``<owner>/<draft>/inputs/<input_id>/<name>`` are candidates for the
        orphan sweep — files directly under the draft dir or nested one
        level deeper are not the exact-rule shape and must stay."""
        canonical = self._write_finalized_file(
            "7/3/inputs/10/keep.txt", b"owed by a row"
        )
        under_draft = self._write_finalized_file(
            "7/3/stray.txt", b"directly under the draft dir"
        )
        nested = self._write_finalized_file(
            "7/3/inputs/10/extra/nested.txt", b"one level too deep"
        )

        counts = self.storage.reconcile(
            {LIVE_PAIR}, max_age_seconds=3600, valid_input_relpaths={canonical}
        )

        self.assertTrue(
            (self.root / "7" / "3" / "stray.txt").is_file(),
            "a file directly under the draft dir (outside inputs/<id>/<name>) "
            f"must not be removed (counts {counts})",
        )
        self.assertTrue(
            (
                self.root / "7" / "3" / "inputs" / "10" / "extra" / "nested.txt"
            ).is_file(),
            "a file nested deeper than inputs/<id>/<name> must not be removed "
            f"(counts {counts})",
        )

    def test_junction_inside_inputs_is_not_followed_and_target_untouched(self):
        """GREEN at base, must stay green after the fix: a directory junction
        inside inputs/ pointing OUTSIDE the storage root must not be
        followed by the sweep — the target's bytes stay untouched. Uses a
        Windows junction (``cmd /c mklink /J`` — no privilege needed); falls
        back to a symlink; skips when the platform can create neither."""
        import platform
        import shutil as _shutil
        import subprocess
        import tempfile

        outside_dir = tempfile.mkdtemp()
        try:
            target_file = pathlib.Path(outside_dir) / "secret.txt"
            target_file.write_bytes(b"bytes that live outside the storage root")

            link_parent = self.root / "7" / "3" / "inputs" / "10"
            link_parent.mkdir(parents=True, exist_ok=True)
            link = link_parent / "escape"
            if platform.system() == "Windows":
                dest = subprocess.list2cmdline([str(link)])
                src = subprocess.list2cmdline([outside_dir])
                made = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", dest, src],
                    capture_output=True,
                )
                if made.returncode != 0:
                    self.skipTest(
                        "junction creation unavailable on this platform: "
                        f"{made.stderr!r}"
                    )
            else:
                try:
                    os.symlink(outside_dir, link, target_is_directory=True)
                except (OSError, NotImplementedError):
                    self.skipTest("symlinks unavailable on this platform")
            try:
                canonical = self._write_finalized_file(
                    "7/3/inputs/10/keep.txt", b"owed by a row"
                )
                self.storage.reconcile(
                    {LIVE_PAIR},
                    max_age_seconds=3600,
                    valid_input_relpaths={canonical},
                )
                self.assertTrue(
                    target_file.is_file(),
                    "the sweep must not follow a junction inside inputs/ — the "
                    "target's bytes outside the storage root must be untouched",
                )
                self.assertEqual(
                    target_file.read_bytes(),
                    b"bytes that live outside the storage root",
                    "the junction target's bytes must be unchanged",
                )
            finally:
                # Remove the LINK only (never recurse through it).
                if platform.system() == "Windows":
                    subprocess.run(
                        ["cmd", "/c", "rmdir", subprocess.list2cmdline([str(link)])],
                        capture_output=True,
                    )
                else:
                    if link.is_symlink():
                        link.unlink()
        finally:
            _shutil.rmtree(outside_dir, ignore_errors=True)

    def test_internal_junction_alias_is_skipped_and_row_backed_file_survives(
        self,
    ):
        """Review PRR-005 (CHECK_WRONG amendment): an INTERNAL junction
        ``inputs/10 -> inputs/11`` (both inside the storage root) must not
        turn the lexical candidate ``inputs/10/<name>`` into a deletion of
        the row-backed live file at ``inputs/11/<name>``. RED at the pre-fix
        review head (the sweep deleted the row-backed file through the
        junction and logged a false "reaped" count), GREEN after the fix
        (reparse candidates are skipped via the lstat reparse tag and a
        resolved-path identity check). Uses a Windows junction
        (``cmd /c mklink /J``); skips when the platform cannot create one."""
        import platform
        import subprocess

        link_parent = self.root / "7" / "3" / "inputs" / "10"
        real_dir = self.root / "7" / "3" / "inputs" / "11"
        real_dir.mkdir(parents=True, exist_ok=True)
        link_parent.mkdir(parents=True, exist_ok=True)
        link = link_parent / "alias"
        if platform.system() == "Windows":
            dest = subprocess.list2cmdline([str(link)])
            src = subprocess.list2cmdline([str(real_dir)])
            made = subprocess.run(
                ["cmd", "/c", "mklink", "/J", dest, src],
                capture_output=True,
            )
            if made.returncode != 0:
                self.skipTest(
                    f"junction creation unavailable on this platform: {made.stderr!r}"
                )
        else:
            self.skipTest(
                "the deletion-through-internal-junction shape is "
                "Windows-specific (mklink /J); CI's ubuntu leg exercises the "
                "symlink-skip arms instead"
            )
        live = real_dir / "live.txt"
        live.write_bytes(b"owed by a live row")
        canonical = self._write_finalized_file(
            "7/3/inputs/11/live.txt", b"owed by a live row"
        )
        # The junction dir must not carry its own candidate files.
        try:
            self.storage.reconcile(
                {LIVE_PAIR},
                max_age_seconds=3600,
                valid_input_relpaths={canonical},
            )
            self.assertTrue(
                live.is_file(),
                "PRR-005: the sweep must not delete the row-backed live file "
                "through an internal junction alias — the lexical candidate "
                "inputs/10/live.txt resolves to a different file than the "
                "valid inputs/11/live.txt",
            )
            self.assertEqual(
                live.read_bytes(),
                b"owed by a live row",
                "the row-backed file's bytes must be unchanged",
            )
        finally:
            # Remove the LINK only (never recurse through it).
            subprocess.run(
                ["cmd", "/c", "rmdir", subprocess.list2cmdline([str(link)])],
                capture_output=True,
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
