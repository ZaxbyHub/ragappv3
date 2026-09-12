"""Opt-in live-judge adapter for answer-quality evaluation (issue #237, AC10).

This module is deliberately network-free: the judge *client* (whatever
scores prompts) is injected by the caller, so the adapter itself performs no
I/O. Real judges are operator tooling only — they are never invoked from
deterministic CI, and every judgment records its judge identity
(provider/model/prompt version), bias controls (seeded position
randomization and an optional cross-family judge), and human-calibration
metadata, so a score is always interpretable against how it was produced.

E09 note (issue #237 frontier-audit amendment): judge-bias controls —
deterministic position randomization and cross-family judge configuration —
land here; evaluating over OTLP traces is deferred to issue #518 (OTel
telemetry), which owns the trace carrier.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Optional, Protocol, Sequence


@dataclass(frozen=True)
class JudgeIdentity:
    """Who judged: provider, model, and prompt version."""

    provider: str
    model: str
    prompt_version: str


@dataclass(frozen=True)
class JudgeBiasControls:
    """Bias controls applied when presenting candidates to the judge.

    ``position_randomization`` shuffles the presentation order with a seeded
    PRNG (``random_seed``) so the same seed always produces the same order —
    reproducible bias control, not hidden nondeterminism.
    ``cross_family_judge`` names a second judge from a different model family
    used (by the operator) to cross-check family-specific bias.
    """

    position_randomization: bool
    random_seed: int
    cross_family_judge: Optional[str] = None


@dataclass(frozen=True)
class JudgeCalibration:
    """Human-calibration metadata for this judge configuration."""

    sample_size: int = 0
    agreement_rate: Optional[float] = None
    calibrated_on: Optional[str] = None


@dataclass(frozen=True)
class CandidateOutput:
    """One candidate answer under judgment."""

    id: str
    query: str
    answer: str


@dataclass(frozen=True)
class Judgment:
    """One judged candidate, carrying its full provenance."""

    candidate_id: str
    score: float
    identity: JudgeIdentity
    bias_controls: JudgeBiasControls
    human_calibration: Optional[JudgeCalibration]


class JudgeClient(Protocol):
    """Duck-typed scoring client injected by the caller.

    Real deployments back this with a live LLM provider (operator tooling);
    tests back it with a deterministic fake. The adapter never opens a
    network connection itself.
    """

    def score(self, prompt: str) -> float:
        """Score a prompt and return a numeric score."""
        ...


_JUDGE_PROMPT_TEMPLATE = (
    "judge_prompt_version={prompt_version}\n"
    "provider={provider}\n"
    "model={model}\n"
    "candidate_id: {candidate_id}\n"
    "query: {query}\n"
    "answer: {answer}\n"
    "score the answer for faithfulness and relevance on [0, 1]"
)


class JudgeAdapter:
    """Adapter that presents candidates to an injected judge client.

    Synchronous and offline: all bias controls are deterministic, and the
    presentation order for a fixed seed is identical across instances.
    """

    def __init__(
        self,
        client: JudgeClient,
        identity: JudgeIdentity,
        bias_controls: JudgeBiasControls,
        human_calibration: Optional[JudgeCalibration] = None,
    ) -> None:
        self._client = client
        self._identity = identity
        self._bias_controls = bias_controls
        self._human_calibration = human_calibration

    def _presentation_order(self, candidates: Sequence[CandidateOutput]) -> list:
        ordered = list(candidates)
        if self._bias_controls.position_randomization:
            # Deterministic by design: a seeded PRNG is the bias control
            # (same seed -> same presentation order), not a security function.
            rng = random.Random(self._bias_controls.random_seed)  # nosec B311
            rng.shuffle(ordered)
        return ordered

    def _build_prompt(self, candidate: CandidateOutput) -> str:
        return _JUDGE_PROMPT_TEMPLATE.format(
            prompt_version=self._identity.prompt_version,
            provider=self._identity.provider,
            model=self._identity.model,
            candidate_id=candidate.id,
            query=candidate.query,
            answer=candidate.answer,
        )

    def judge_candidates(
        self, candidates: Sequence[CandidateOutput]
    ) -> list[Judgment]:
        """Judge every candidate exactly once, in the (seeded) presentation order.

        Each returned judgment carries the judge identity, the bias controls
        in force, and the human-calibration metadata, so no score can be
        mistaken for an uncalibrated or uncontrolled measurement.
        """
        judgments: list[Judgment] = []
        for candidate in self._presentation_order(candidates):
            prompt = self._build_prompt(candidate)
            score = float(self._client.score(prompt))
            judgments.append(
                Judgment(
                    candidate_id=candidate.id,
                    score=score,
                    identity=self._identity,
                    bias_controls=self._bias_controls,
                    human_calibration=self._human_calibration,
                )
            )
        return judgments


__all__ = [
    "CandidateOutput",
    "JudgeAdapter",
    "JudgeBiasControls",
    "JudgeCalibration",
    "JudgeIdentity",
    "Judgment",
]
