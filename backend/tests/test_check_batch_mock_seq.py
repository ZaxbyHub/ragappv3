"""Issue #740 (AC15): scripts/check_batch_mock_seq.py is test-pinned.

Pins the pure scanner: contract-accurate rows pass, a seq-less mock row and
a no-mock-at-all spec both fail (C-BATCHSEQ-1/2), plus the real-repo happy
path through find_offenders.
"""

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "check_batch_mock_seq.py"

GOOD_SOURCE = """
import { api } from "@/lib/api/core";

it("saves", async () => {
  api.addChatMessagesBatch.mockResolvedValue([
    { id: "r1", seq: 1, turn: "a" },
    { id: "r2", seq: 2, turn: "b" },
  ]);
  await send();
});
"""

SEQLESS_SOURCE = """
import { api } from "@/lib/api/core";

it("saves", async () => {
  api.addChatMessagesBatch.mockResolvedValue([
    { id: "r1", turn: "a" },
  ]);
  await send();
});
"""

STRINGLY_SOURCE = """
import { api } from "@/lib/api/core";

it("saves", async () => {
  api.addChatMessagesBatch.mockResolvedValue([
    { id: "r1", seq: "1", turn: "a" },
  ]);
  await send();
});
"""

NO_MOCK_SOURCE = """
import { api } from "@/lib/api/core";

it("saves", async () => {
  await send();
});
"""


def _load():
    spec = importlib.util.spec_from_file_location("check_batch_mock_seq_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_scanner_accepts_numeric_seq_rows():
    mod = _load()
    found, missing = mod.scan_source(GOOD_SOURCE)
    assert found is True and missing is False


def test_scanner_flags_seqless_row():
    mod = _load()
    found, missing = mod.scan_source(SEQLESS_SOURCE)
    assert found is True and missing is True


def test_scanner_flags_stringly_typed_seq():
    mod = _load()
    found, missing = mod.scan_source(STRINGLY_SOURCE)
    assert found is True and missing is True


def test_scanner_flags_spec_without_batch_mock():
    mod = _load()
    found, missing = mod.scan_source(NO_MOCK_SOURCE)
    assert found is False and missing is False  # C-BATCHSEQ-2 via find_offenders


def test_find_offenders_passes_on_repo():
    mod = _load()
    assert mod.find_offenders(REPO) == []


def test_find_offenders_flags_seeded_target(tmp_path, monkeypatch):
    mod = _load()
    target_rel = "frontend/src/hooks/__seed__.test.ts"
    (tmp_path / "frontend/src/hooks").mkdir(parents=True)
    (tmp_path / target_rel).write_text(SEQLESS_SOURCE, encoding="utf-8")
    monkeypatch.setattr(mod, "TARGETS", (target_rel,))
    assert mod.find_offenders(tmp_path) == [target_rel]
