"""Regression tests for multimodal atom claim ownership outcomes."""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.multimodal_enrichment import ArtifactEnrichmentService
from tests.multimodal_enrichment_fixture import (
    FakeEnrichClient,
    MultimodalEnrichmentFixture,
)


class TestMultimodalClaimOutcomes(MultimodalEnrichmentFixture):
    def test_claim_rejection_returns_in_progress_without_provider_call(self) -> None:
        """A competing claim must stop before asset loading or provider egress."""
        with patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"}):
            client = FakeEnrichClient(
                base_url="http://127.0.0.1:11434",
                response='{"description": "unused", "retrieval_aids": []}',
            )
            service = ArtifactEnrichmentService(pool=self._pool(), client=client)
            with patch.object(self._st, "claim_atom_stage", return_value=False):
                result = asyncio.run(
                    service._enrich_atom(
                        atom=self._atom_dict(),
                        vault_id=self.vault_id,
                        file_id=self.file_id,
                        generation_hash=self.generation_hash,
                        neighbors=([], []),
                        document_title="doc",
                    )
                )

        self.assertEqual(result, {"atom_id": self.atom_id, "outcome": "in_progress"})
        self.assertEqual(client.calls, 0)

    def test_enrich_atoms_accounts_for_in_progress_without_retrying(self) -> None:
        """Concurrent ownership is observable but does not schedule duplicate work."""
        service = ArtifactEnrichmentService(pool=MagicMock(), client=MagicMock())
        atom = {"atom_pk": 1, "kind": "image"}
        service._load_ordered_atoms = MagicMock(return_value=[atom])
        service._actionable_atom_pks = MagicMock(return_value={1})
        service._enrich_atom = AsyncMock(
            return_value={"atom_id": "atom-1", "outcome": "in_progress"}
        )

        result = asyncio.run(
            service.enrich_atoms(
                vault_id=1,
                file_id=2,
                generation_hash="gen",
                document_title="doc",
            )
        )

        self.assertEqual(
            result,
            {"proxy_records": [], "retryable": 0, "in_progress": 1},
        )
