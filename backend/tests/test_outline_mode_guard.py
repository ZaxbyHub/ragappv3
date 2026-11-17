"""Item 1c: the incident project regression — compose column, reference-only input,
model-emitted rewrite mode must fail the Outline stage."""
import pytest

from app.services.draft_pipeline import CompileFailure
from app.services.draft_prompts import (
    OutlineArtifact,
    OutlineCritic,
    OutlineSection,
)


class _Ctx:
    """Project 20's exact state: mode column 'compose', brief strength
    'moderate', one input with role 'reference' (no manuscript)."""
    mode = "compose"
    manuscript_inputs = []
    inputs = [type("Input", (), {"role": "reference", "character_count": 1426})()]
    brief_json = (
        '{"piece_type":"brief","transformation_strength":"moderate",'
        '"target_words":200}'
    )


def _artifact(mode: str, sections=None) -> OutlineArtifact:
    return OutlineArtifact(
        mode=mode,
        sections=sections if sections is not None else [],
        voice_rules=["Tone: clear and direct."],
        critic=OutlineCritic(verdict="approved", findings=[]),
    )


def test_model_emitted_rewrite_with_reference_only_inputs_fails():
    """The model emitting mode='rewrite' with no manuscript input must fail
    the outline stage (the incident project's exact DB state)."""
    from app.services import draft_pipeline as dp

    outline = _artifact("rewrite")
    with pytest.raises(CompileFailure) as exc:
        dp._guard_outline_mode_and_sections(outline, _Ctx())
    assert exc.value.code in ("manuscript_required", "rewrite_requires_manuscript")


def test_zero_sections_fails_even_in_compose():
    from app.services import draft_pipeline as dp

    with pytest.raises(CompileFailure) as exc:
        dp._guard_outline_mode_and_sections(_artifact("compose"), _Ctx())
    assert exc.value.code == "outline_empty"


def test_compose_with_sections_passes():
    from app.services import draft_pipeline as dp

    ok = OutlineArtifact(
        mode="compose",
        sections=[OutlineSection(
            section_id="S1", heading="H", purpose="p", target_words=40,
            must_preserve=[], acceptance_checks=[], evidence_labels=[],
        )],
        voice_rules=[],
        critic=OutlineCritic(verdict="approved", findings=[]),
    )
    dp._guard_outline_mode_and_sections(ok, _Ctx())
