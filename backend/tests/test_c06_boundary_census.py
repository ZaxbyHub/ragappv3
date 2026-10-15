"""Acceptance checks for issue #711 (C-06) — default-prompt boundary census.

These checks freeze the #711 acceptance criteria for the default system
prompt of PromptBuilderService:

- AC3: the SECURITY BOUNDARY sentence of the default prompt must name
  ``<kms_evidence>`` among the declared untrusted-content wrapper tags. RED
  at the #711 base, GREEN once #711 lands.
- AC4: every wrapper tag the formatters emit via ``<tag>{...}`` f-string
  interpolation must be declared in that SECURITY BOUNDARY sentence. RED at
  the #711 base (``<kms_evidence>`` is emitted but not declared), GREEN once
  #711 lands.
"""

import re
from pathlib import Path

from app.services.prompt_builder import PromptBuilderService

PROMPT_BUILDER_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "services" / "prompt_builder.py"
)

# Opening tags emitted as f-string wrappers, e.g. "<memory>{...}</memory>".
_EMITTED_TAG_RE = re.compile(r"<([a-zA-Z_][a-zA-Z0-9_]*)>\{")

# Any tag name mentioned in the boundary sentence, e.g. "<user_query>".
_DECLARED_TAG_RE = re.compile(r"<([a-zA-Z_][a-zA-Z0-9_]*)>")


def _boundary_slice(prompt: str) -> str:
    """The prompt from the SECURITY BOUNDARY marker onward."""
    return prompt[prompt.index("SECURITY BOUNDARY"):]


def test_default_prompt_boundary_names_kms_evidence():
    """AC3: the boundary sentence must name <kms_evidence>.

    KMS excerpts are user-curated, untrusted content wrapped in
    ``<kms_evidence>`` tags by format_kms_evidence; the SECURITY BOUNDARY
    sentence that tells the model tagged content is data must name that tag.
    RED at the #711 base, GREEN once #711 lands.
    """
    prompt = PromptBuilderService()._default_system_prompt()
    boundary = _boundary_slice(prompt)
    named = "<kms_evidence>" in boundary
    assert named is True, (
        "the SECURITY BOUNDARY sentence must name <kms_evidence> among the "
        "declared untrusted-content wrapper tags"
    )


def test_every_emitted_wrapper_tag_is_declared():
    """AC4: census — every emitted <tag>{...} wrapper must be declared.

    Scans prompt_builder.py source for wrapper tags emitted via f-string
    interpolation (``<tag>{``) and requires each to appear in the SECURITY
    BOUNDARY sentence of the default system prompt. RED at the #711 base
    (missing = {"kms_evidence"}), GREEN once #711 lands.
    """
    source = PROMPT_BUILDER_PATH.read_text(encoding="utf-8")
    emitted = set(_EMITTED_TAG_RE.findall(source))

    prompt = PromptBuilderService()._default_system_prompt()
    boundary = _boundary_slice(prompt)
    declared = set(_DECLARED_TAG_RE.findall(boundary))

    missing = emitted - declared
    assert len(missing) == 0, (
        f"wrapper tags emitted into prompts but not declared in the "
        f"SECURITY BOUNDARY sentence: {sorted(missing)}"
    )
