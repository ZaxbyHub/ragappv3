"""Route-behavior pins for the /folders/move batch cap and rate limit.

Added in the issue-#700 feedback round (review PRR-007 / spr2 F-6): the move
route now bounds `file_ids` (max_length=1000, aligned with the documents
per-page ceiling the UI selects against) and rate-limits the route, so a
huge batch cannot hold move_documents' write transaction for an arbitrary
IN-scan. These tests pin both behaviors at the HTTP surface:

- a 1000-id payload is accepted (200) while a 1001-id payload is a 422 —
  the boundary sits exactly at the UI's select-all ceiling;
- the 11th move inside the admin rate-limit window is a 429.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_kms_routes import KMSFixTestBase  # noqa: E402


class TestFolderMoveLimits(KMSFixTestBase):
    def test_move_batch_cap_boundary_1000_accepted_1001_rejected(self):
        # 1000 ids: exactly the UI's select-all ceiling -> accepted (files
        # beyond the two seeded ones are dropped as out-of-vault by the
        # store; the route's only concern is the cap).
        ok = self.client.post(
            "/api/folders/move",
            json={"vault_id": 2, "file_ids": list(range(1, 1001)), "folder_id": None},
            headers=self._write_headers(),
        )
        self.assertEqual(ok.status_code, 200)
        # 1001 ids: one past the ceiling -> 422 validation.
        over = self.client.post(
            "/api/folders/move",
            json={"vault_id": 2, "file_ids": list(range(1, 1002)), "folder_id": None},
            headers=self._write_headers(),
        )
        self.assertEqual(over.status_code, 422)

    def test_move_route_rate_limited(self):
        codes = []
        for _ in range(11):
            resp = self.client.post(
                "/api/folders/move",
                json={"vault_id": 2, "file_ids": [600], "folder_id": None},
                headers=self._write_headers(),
            )
            codes.append(resp.status_code)
        self.assertEqual(codes[:10], [200] * 10)
        self.assertEqual(codes[10], 429)
