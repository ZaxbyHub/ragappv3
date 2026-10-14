"""HTTP-level pins for the export Content-Disposition header (issue #789 review F-001).

The RFC 6266 ``filename*`` parameter added by #789 is the only mechanism by
which a non-ASCII draft title survives to a modern client, and it had **no
assertion anywhere**: the unit checks in ``test_n05_export_filename.py`` cover
the ASCII helper, and because that helper ASCII-folds, its CJK case produces
the same ``draft-...`` fallback at base and at head — so it pins the fold, not
the preservation. These tests read the real response header, which is the
contract a browser actually consumes.

Kept in its own module (not appended to an existing export test class) so the
frozen acceptance files stay byte-stable.
"""

from tests.draft_room.test_draft_compile_routes import TestExportMatrix


class TestN05CExportFilenameHeader(TestExportMatrix):
    """Inherits the real export harness (draft, completed job, revision)."""

    def _export_disposition(self, title, *, ack=True):
        draft_id = self._draft_with_ready_input(title=title)
        job_id = self._seed_completed_job(draft_id)
        revision_id = self._seed_current_revision(
            draft_id,
            job_id=job_id,
            content_md="# Draft\n\nbody",
            fact_status="not_run",
        )
        self._set_draft_status(draft_id, "needs_review")
        resp = self._export(draft_id, revision_id, ack=ack)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.headers["content-disposition"]

    def test_filename_star_carries_the_percent_encoded_non_ascii_title(self):
        """A CJK title survives end to end in the ``filename*`` parameter."""
        disposition = self._export_disposition("季度报告")

        self.assertIn("filename*=UTF-8''", disposition)
        # Percent-encoded UTF-8 of 季度报告 — the exact bytes a client decodes.
        self.assertIn(
            "%E5%AD%A3%E5%BA%A6%E6%8A%A5%E5%91%8A",
            disposition,
            f"the raw title must survive in filename*: {disposition}",
        )
        # …and the RFC 6266 suffix still rides along after it.
        self.assertTrue(
            disposition.rstrip('"').endswith(".md"),
            f"filename* must keep the revision/tag suffix: {disposition}",
        )

    def test_filename_star_percent_encodes_control_and_separator_characters(self):
        """Titles cannot inject structure into the parameter or the header."""
        disposition = self._export_disposition('A/B "quoted"\r\nX-Injected: 1')

        # No raw control characters survive in the header value itself.
        self.assertNotIn("\r", disposition)
        self.assertNotIn("\n", disposition)
        self.assertIn("filename*=UTF-8''", disposition)
        star = disposition.split("filename*=UTF-8''", 1)[1]
        # The separator is folded to "_" BEFORE quoting, so no path separator
        # reaches the parameter at all; quotes and CR/LF are percent-encoded.
        self.assertNotIn('"', star)
        self.assertNotIn("/", star)
        self.assertNotIn("\\", star)
        self.assertIn("A_B", star)  # A/B → A_B (content, not path)
        self.assertIn("%22", star)  # " → %22
        self.assertIn("%0D%0A", star)  # CRLF → percent-encoded
        self.assertNotIn("\r", star)
        self.assertNotIn("\n", star)

    def test_ascii_filename_parameter_is_unchanged_for_plain_titles(self):
        """The legacy parameter stays byte-identical for a plain ASCII title
        (RFC 6266 gives ``filename*`` precedence; ``filename`` must not
        regress for clients that only read the old one). The two parameters
        differ only in how they spell the same characters — the ASCII one
        underscores the space, the UTF-8 one percent-encodes it."""
        disposition = self._export_disposition("Quarterly Report")

        self.assertIn('filename="Quarterly_Report-rev', disposition)
        self.assertIn("filename*=UTF-8''Quarterly%20Report-rev", disposition)
