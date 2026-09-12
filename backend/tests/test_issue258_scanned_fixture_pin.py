"""Phase 4.6 final-critic publication condition 4: pin the scanned-page fixture.

The C12 expected-set predates scanned_page.pdf, so its deletion would only be
caught by the next nightly. This additive PR-gate pin fails immediately if the
image-only fixture disappears or loses its zero-text shape.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "real_docs", "scanned_page.pdf"
)


class TestScannedPageFixturePinned(unittest.TestCase):
    def test_scanned_page_fixture_exists_and_is_image_only(self):
        with open(FIXTURE, "rb") as fh:
            data = fh.read()
        self.assertTrue(data.startswith(b"%PDF"), "not a PDF")
        self.assertIn(b"/XObject", data, "expected an embedded image XObject")
        self.assertNotIn(b"Tj", data, "fixture must contain no text-showing operators")


if __name__ == "__main__":
    unittest.main()
