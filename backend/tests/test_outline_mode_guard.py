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


# -- call sites inside the real _CompileRun._stage_outline ------------------
# The helper tests above do not prove _stage_outline CALLS the guard. These
# drive the real stage on a bare instance (built with __new__, collaborators
# stubbed) down both its paths: the fresh model call and checkpoint reuse.


def _bare_outline_run(*, outline: OutlineArtifact, reuse: bool):
    import types

    from app.services import draft_pipeline as dp

    run = dp._CompileRun.__new__(dp._CompileRun)
    run._ctx = types.SimpleNamespace(
        mode="compose",
        manuscript_inputs=[],
        compile_world_sha256="w",
        prompt_bundle_version="v",
        brief_hash="b",
        brief_json="{}",
        max_sections=10,
    )
    run._packet = OutlineArtifact(
        mode="compose", sections=[], voice_rules=[],
        critic=OutlineCritic(verdict="approved", findings=[]),
    )
    run._source_snapshot_sha256 = "s"
    run._outline = None
    persisted = []

    async def _persist(*args, **kwargs):
        persisted.append(args)

    async def _call_model(**kwargs):
        return outline, None

    record = types.SimpleNamespace(artifact_json=outline.model_dump_json())
    run._reusable = lambda stage, input_sha: record if reuse else None
    run._call_model = _call_model
    run._render_context = lambda upstream, **extra: {}
    run._persist_stage = _persist
    return run, persisted


def test_stage_outline_fresh_zero_sections_fails_outline_empty():
    import asyncio

    run, persisted = _bare_outline_run(outline=_artifact("compose"), reuse=False)
    with pytest.raises(CompileFailure) as exc:
        asyncio.run(run._stage_outline())
    assert exc.value.code == "outline_empty"
    assert run._outline is None
    assert persisted == []


def test_stage_outline_reused_checkpoint_with_zero_sections_fails_outline_empty():
    import asyncio

    run, _ = _bare_outline_run(outline=_artifact("compose"), reuse=True)
    with pytest.raises(CompileFailure) as exc:
        asyncio.run(run._stage_outline())
    assert exc.value.code == "outline_empty"
    assert run._outline is None
