"""Tests for the opt-in live-judge adapter (issue #237, AC10).

Offline only: the judge client is a deterministic recording fake; the
adapter itself performs no network I/O and is never invoked against a live
endpoint from the test suite.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.eval_judge import (
    CandidateOutput,
    JudgeAdapter,
    JudgeBiasControls,
    JudgeCalibration,
    JudgeIdentity,
)


class RecordingClient:
    """Deterministic in-memory judge client for offline testing."""

    def __init__(self) -> None:
        self.prompts = []

    def score(self, prompt: str) -> float:
        self.prompts.append(prompt)
        return 0.75


IDENTITY = JudgeIdentity(provider="fake", model="fake-judge-1", prompt_version="v1")
CONTROLS = JudgeBiasControls(
    position_randomization=True, random_seed=237, cross_family_judge="other-family"
)
CALIBRATION = JudgeCalibration(
    sample_size=40, agreement_rate=0.9, calibrated_on="human-panel-a"
)


def _candidates():
    return [
        CandidateOutput(id="cand-%d" % i, query="q%d" % i, answer="answer %d" % i)
        for i in range(5)
    ]


class TestJudgeAdapter(unittest.TestCase):
    def test_deterministic_presentation_order_for_fixed_seed(self):
        client_a, client_b = RecordingClient(), RecordingClient()
        adapter_a = JudgeAdapter(client_a, IDENTITY, CONTROLS, CALIBRATION)
        adapter_b = JudgeAdapter(client_b, IDENTITY, CONTROLS, CALIBRATION)
        judgments_a = adapter_a.judge_candidates(_candidates())
        judgments_b = adapter_b.judge_candidates(_candidates())
        self.assertEqual(client_a.prompts, client_b.prompts)
        self.assertEqual(
            [j.candidate_id for j in judgments_a],
            [j.candidate_id for j in judgments_b],
        )

    def test_judgments_cover_every_candidate_exactly_once(self):
        adapter = JudgeAdapter(RecordingClient(), IDENTITY, CONTROLS, CALIBRATION)
        judgments = adapter.judge_candidates(_candidates())
        self.assertEqual(
            sorted(j.candidate_id for j in judgments),
            sorted(c.id for c in _candidates()),
        )

    def test_every_judgment_carries_provenance(self):
        adapter = JudgeAdapter(RecordingClient(), IDENTITY, CONTROLS, CALIBRATION)
        for judgment in adapter.judge_candidates(_candidates()):
            self.assertEqual(judgment.identity, IDENTITY)
            self.assertEqual(judgment.bias_controls, CONTROLS)
            self.assertEqual(judgment.human_calibration, CALIBRATION)
            self.assertIsInstance(judgment.score, float)

    def test_no_randomization_preserves_input_order(self):
        controls = JudgeBiasControls(position_randomization=False, random_seed=1)
        adapter = JudgeAdapter(RecordingClient(), IDENTITY, controls)
        judgments = adapter.judge_candidates(_candidates())
        self.assertEqual(
            [j.candidate_id for j in judgments],
            [c.id for c in _candidates()],
        )

    def test_client_sees_candidate_content(self):
        client = RecordingClient()
        adapter = JudgeAdapter(client, IDENTITY, CONTROLS, CALIBRATION)
        candidates = _candidates()
        adapter.judge_candidates(candidates)
        for candidate in candidates:
            self.assertTrue(
                any(candidate.id in p and candidate.answer in p for p in client.prompts)
            )


if __name__ == "__main__":
    unittest.main()
