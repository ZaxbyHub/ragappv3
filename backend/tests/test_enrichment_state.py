"""Tests for atom-scoped enrichment state machine + fingerprints (issue #461)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:  # pragma: no cover - CI installs no lancedb
    import lancedb  # noqa: F401
except ImportError:  # pragma: no cover
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

from app.models.database import init_db
from app.services import enrichment_state as st
from app.services.document_artifacts import AtomKind, make_atom_id


class StateTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "test.db")
        init_db(self.db_path)
        self.conn = _connect(self.db_path)
        # Seed a file, vault, and one image atom.
        self.conn.execute(
            "INSERT INTO vaults (name) VALUES ('v')"
        )
        vault_id = self.conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()["id"]
        self.conn.execute(
            "INSERT INTO files (file_name, file_path, file_hash, file_size, vault_id, status) "
            "VALUES ('a.png', '/x/a.png', 'hash1', 0, ?, 'indexed')",
            (vault_id,),
        )
        self.file_id = self.conn.execute("SELECT id FROM files LIMIT 1").fetchone()["id"]
        self.gen = "genhash123"
        self.atom_id = make_atom_id(self.file_id, self.gen, 0)
        atom_pk = self.conn.execute(
            "INSERT INTO document_atoms (atom_id, schema_version, file_id, generation_hash, "
            "ordinal, kind, raw_text) VALUES (?, 1, ?, ?, 0, 'image', 'ocr') RETURNING id",
            (self.atom_id, self.file_id, self.gen),
        ).fetchone()["id"]
        self.atom_pk = atom_pk
        self.conn.commit()

    def tearDown(self) -> None:
        try:
            self.conn.close()
        finally:
            self._tmp.cleanup()

    def _fp(self, **kw) -> str:
        defaults = dict(
            generation_hash=self.gen,
            asset_sha="sha",
            atom_schema_version=1,
            impl_version="1",
            prompt_version="v1",
            model="m",
            logical_mode="thinking",
            response_schema_version="v1",
            max_pixels=1,
            max_asset_bytes=1,
        )
        defaults.update(kw)
        return st.compute_input_fingerprint(**defaults)


def _connect(path):
    import sqlite3

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class TestFingerprint(unittest.TestCase):
    def test_deterministic_and_input_sensitive(self) -> None:
        a = st.compute_input_fingerprint(
            generation_hash="g", asset_sha="s", atom_schema_version=1, impl_version="1",
            prompt_version="v1", model="m", logical_mode="thinking",
            response_schema_version="v1", max_pixels=1, max_asset_bytes=1,
        )
        b = st.compute_input_fingerprint(
            generation_hash="g", asset_sha="s", atom_schema_version=1, impl_version="1",
            prompt_version="v1", model="m", logical_mode="thinking",
            response_schema_version="v1", max_pixels=1, max_asset_bytes=1,
        )
        self.assertEqual(a, b)
        c = st.compute_input_fingerprint(
            generation_hash="g2", asset_sha="s", atom_schema_version=1, impl_version="1",
            prompt_version="v1", model="m", logical_mode="thinking",
            response_schema_version="v1", max_pixels=1, max_asset_bytes=1,
        )
        self.assertNotEqual(a, c)


class TestStageMachine(StateTestBase):
    def test_claim_and_succeed(self) -> None:
        fp = self._fp()
        with self.conn:
            st.claim_atom_stage(
                self.conn, file_id=self.file_id, generation_hash=self.gen,
                atom_pk=self.atom_pk, stage=st.ENRICH_STAGE, input_fingerprint=fp,
                implementation_version="1", model_id="m", prompt_id="v1", config_id="v1",
            )
            row = st._atom_stage_status(
                self.conn, file_id=self.file_id, generation_hash=self.gen,
                atom_pk=self.atom_pk, stage=st.ENRICH_STAGE,
            )
            self.assertEqual(row["status"], st.RUNNING)
            ok = st.complete_atom_stage(
                self.conn, file_id=self.file_id, generation_hash=self.gen,
                atom_pk=self.atom_pk, stage=st.ENRICH_STAGE, input_fingerprint=fp,
                status=st.SUCCEEDED, attempts=1,
            )
            self.assertTrue(ok)
            st.upsert_derived(
                self.conn, file_id=self.file_id, generation_hash=self.gen,
                atom_id=self.atom_id, input_fingerprint=fp, description="d",
                retrieval_aids=["a"], prompt_version="v1", schema_version="v1",
                impl_version="1", provider_snapshot={"base_host": "h"},
            )
            self.conn.commit()
        derived = st.load_derived(
            self.conn, file_id=self.file_id, generation_hash=self.gen, atom_id=self.atom_id
        )
        self.assertEqual(derived["description"], "d")
        self.assertEqual(derived["retrieval_aids"], ["a"])

    def test_stale_fingerprint_rejection(self) -> None:
        fp1 = self._fp(generation_hash="genA")
        with self.conn:
            st.claim_atom_stage(
                self.conn, file_id=self.file_id, generation_hash=self.gen,
                atom_pk=self.atom_pk, stage=st.ENRICH_STAGE, input_fingerprint=fp1,
                implementation_version="1", model_id="m", prompt_id="v1", config_id="v1",
            )
            # A completion with a DIFFERENT fingerprint (source generation moved) is rejected.
            fp2 = self._fp(generation_hash="genB")
            ok = st.complete_atom_stage(
                self.conn, file_id=self.file_id, generation_hash=self.gen,
                atom_pk=self.atom_pk, stage=st.ENRICH_STAGE, input_fingerprint=fp2,
                status=st.SUCCEEDED, attempts=1,
            )
            self.assertFalse(ok)
            self.conn.commit()
        row = st._atom_stage_status(
            self.conn, file_id=self.file_id, generation_hash=self.gen,
            atom_pk=self.atom_pk, stage=st.ENRICH_STAGE,
        )
        # The running claim is preserved; stale success did not overwrite it.
        self.assertEqual(row["status"], st.RUNNING)

    def test_recovery_reclaims_running(self) -> None:
        with self.conn:
            st.claim_atom_stage(
                self.conn, file_id=self.file_id, generation_hash=self.gen,
                atom_pk=self.atom_pk, stage=st.ENRICH_STAGE,
                input_fingerprint=self._fp(), implementation_version="1",
                model_id="m", prompt_id="v1", config_id="v1",
            )
            self.conn.commit()
        recovered = st.recover_stranded_atom_stages(self.conn)
        self.assertEqual(recovered, 1)
        row = st._atom_stage_status(
            self.conn, file_id=self.file_id, generation_hash=self.gen,
            atom_pk=self.atom_pk, stage=st.ENRICH_STAGE,
        )
        self.assertEqual(row["status"], st.PENDING)

    def test_list_enrichable_only_actionable(self) -> None:
        fp = self._fp()
        with self.conn:
            st.claim_atom_stage(
                self.conn, file_id=self.file_id, generation_hash=self.gen,
                atom_pk=self.atom_pk, stage=st.ENRICH_STAGE, input_fingerprint=fp,
                implementation_version="1", model_id="m", prompt_id="v1", config_id="v1",
            )
            st.complete_atom_stage(
                self.conn, file_id=self.file_id, generation_hash=self.gen,
                atom_pk=self.atom_pk, stage=st.ENRICH_STAGE, input_fingerprint=fp,
                status=st.SUCCEEDED, attempts=1,
            )
            self.conn.commit()
        self.assertEqual(
            st.list_enrichable_atoms(
                self.conn, file_id=self.file_id, generation_hash=self.gen, stage=st.ENRICH_STAGE
            ),
            [],
        )

    def test_aggregate_status_counts(self) -> None:
        fp = self._fp()
        with self.conn:
            st.claim_atom_stage(
                self.conn, file_id=self.file_id, generation_hash=self.gen,
                atom_pk=self.atom_pk, stage=st.ENRICH_STAGE, input_fingerprint=fp,
                implementation_version="1", model_id="m", prompt_id="v1", config_id="v1",
            )
            st.complete_atom_stage(
                self.conn, file_id=self.file_id, generation_hash=self.gen,
                atom_pk=self.atom_pk, stage=st.ENRICH_STAGE, input_fingerprint=fp,
                status=st.FAILED_PERMANENT, error_code="x", attempts=1,
            )
            self.conn.commit()
        counts = st.aggregate_stage_status(
            self.conn, file_id=self.file_id, stage=st.ENRICH_STAGE
        )
        self.assertEqual(counts[st.FAILED_PERMANENT], 1)

    def test_set_proxy_vector_id_is_fingerprint_guarded(self) -> None:
        fp = self._fp()
        with self.conn:
            st.upsert_derived(
                self.conn, file_id=self.file_id, generation_hash=self.gen,
                atom_id=self.atom_id, input_fingerprint=fp, description="d",
                retrieval_aids=["a"], prompt_version="v1", schema_version="v1",
                impl_version="1", provider_snapshot={"base_host": "h"},
            )
            self.conn.commit()

        # Matching fingerprint: persists and returns 1.
        with self.conn:
            updated = st.set_proxy_vector_id(
                self.conn, file_id=self.file_id, generation_hash=self.gen,
                atom_id=self.atom_id, input_fingerprint=fp, proxy_vector_id="vid-1",
            )
            self.conn.commit()
        self.assertEqual(updated, 1)
        stored = self.conn.execute(
            "SELECT proxy_vector_id FROM document_atom_enrichments "
            "WHERE file_id = ? AND generation_hash = ? AND atom_id = ?",
            (self.file_id, self.gen, self.atom_id),
        ).fetchone()
        self.assertEqual(stored["proxy_vector_id"], "vid-1")

        # Stale fingerprint (a concurrent re-enrichment changed the input): the
        # vector id must NOT be pinned to the outdated derived row.
        with self.conn:
            n = st.set_proxy_vector_id(
                self.conn, file_id=self.file_id, generation_hash=self.gen,
                atom_id=self.atom_id, input_fingerprint="stale-fingerprint",
                proxy_vector_id="vid-2",
            )
            self.conn.commit()
        self.assertEqual(n, 0)
        stored = self.conn.execute(
            "SELECT proxy_vector_id FROM document_atom_enrichments "
            "WHERE file_id = ? AND generation_hash = ? AND atom_id = ?",
            (self.file_id, self.gen, self.atom_id),
        ).fetchone()
        self.assertEqual(stored["proxy_vector_id"], "vid-1")


    def test_complete_returns_false_on_zero_row_guarded_update(self) -> None:
        """Critic finding: the return signal must be the UPDATE rowcount, not
        conn.total_changes (which is cumulative across a pooled connection and
        stays >0 after the first write). With no stage row present, the guarded
        UPDATE matches 0 rows and complete must return False even though the
        connection has prior writes."""
        # Prior writes on the same connection make total_changes > 0.
        self.conn.execute(
            "INSERT INTO files (file_name, file_path, file_hash, file_size, vault_id, status) "
            "VALUES ('b.png', '/x/b.png', 'hashX', 0, 1, 'indexed')"
        )
        self.conn.commit()
        assert self.conn.total_changes > 0

        fp = self._fp()
        # No stage row exists for this atom yet, so pre-check passes (existing_fp
        # None) but the guarded UPDATE matches 0 rows.
        ok = st.complete_atom_stage(
            self.conn, file_id=self.file_id, generation_hash=self.gen,
            atom_pk=self.atom_pk, stage=st.ENRICH_STAGE, input_fingerprint=fp,
            status=st.SUCCEEDED, attempts=1,
        )
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
