"""Boilerplate-classifier shape tests (issue #1162, final-critic round 1).

The classifier must distinguish actual licence/copyright NOTICES from
prose and procedures that merely MENTION a licence — under the approved
suppression policy, a false boilerplate tag silently drops the document's
wiki page and KMS entry entirely.
"""

import unittest

from app.services.content_classifiers import (
    classify_blocks,
    document_has_qualifying_items,
    entry_summary,
    extract_procedures,
)

DRIVER_LICENSE_PROCEDURE = (
    "# Driver license renewal\n\n"
    "To renew a driver license, follow these steps:\n\n"
    "1. Fill out the renewal application.\n"
    "2. Pay the license fee at the counter.\n"
    "3. Receive the new license by mail.\n"
)
LICENCE_NOTICE = (
    "Licence\n\n"
    "Copyright 2020 Example Corp. All rights reserved.\n"
    "Redistribution and use in source and binary forms are permitted.\n"
)
COPYRIGHT_PROSE_BLOCK = (
    "Copyright 2020 Example Corp. All rights reserved.\n"
    "Redistribution and use in source and binary forms are permitted, "
    "provided that the above copyright notice appears in all copies.\n"
)
PROCEDURE_MENTIONING_COPYRIGHT = (
    "# Publishing steps\n\n"
    "This procedure explains how to publish under our copyright policy.\n\n"
    "1. Review the draft.\n"
    "2. Attach the licence file.\n"
    "3. Publish the article.\n"
)


class TestBoilerplateShape(unittest.TestCase):
    def test_driver_license_procedure_is_content(self):
        kinds = [b.kind for b in classify_blocks(DRIVER_LICENSE_PROCEDURE)]
        self.assertNotIn("boilerplate", kinds)
        self.assertTrue(document_has_qualifying_items(DRIVER_LICENSE_PROCEDURE))
        self.assertEqual(len(extract_procedures(DRIVER_LICENSE_PROCEDURE)), 1)

    def test_procedure_mentioning_copyright_is_content(self):
        kinds = [b.kind for b in classify_blocks(PROCEDURE_MENTIONING_COPYRIGHT)]
        self.assertNotIn("boilerplate", kinds)
        self.assertTrue(document_has_qualifying_items(PROCEDURE_MENTIONING_COPYRIGHT))

    def test_licence_notice_heading_is_boilerplate(self):
        blocks = classify_blocks(LICENCE_NOTICE)
        self.assertEqual(blocks[0].kind, "boilerplate")

    def test_whole_block_legal_wording_is_boilerplate(self):
        blocks = classify_blocks(COPYRIGHT_PROSE_BLOCK)
        self.assertEqual(blocks[0].kind, "boilerplate")

    def test_legal_notice_is_excluded_from_summary(self):
        body = LICENCE_NOTICE + "\n\nAlice Smith is the AFOMIS Director.\n"
        summary = entry_summary(body)
        self.assertNotIn("Copyright", summary)
        self.assertIn("AFOMIS Director", summary)

    def test_license_procedure_summary_keeps_prose(self):
        summary = entry_summary(DRIVER_LICENSE_PROCEDURE)
        self.assertIn("renew a driver license", summary)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
