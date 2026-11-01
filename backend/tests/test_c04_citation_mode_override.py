"""Issue #709 acceptance check (C04, AC8): citation_mode="disabled" must
actually change a custom system_prompt_override.

``PromptBuilderService.build_messages`` implements the disabled mode as an
exact-substring ``replace(CITATION_INSTRUCTION, "")`` on the effective
prompt. When the effective prompt is a CUSTOM ``system_prompt_override``
that does not contain the CITATION_INSTRUCTION constant verbatim (e.g. an
administrator's own prompt that phrases citation rules differently), the
replace is a no-op: the operator asked for citations OFF and silently got
the citation-instructing prompt anyway.

Contract: with a custom override lacking the exact constant, the system
message built with citation_mode="disabled" must differ from the one
built with citation_mode=None. Pure check — no DB, no HTTP.
"""

from app.services.prompt_builder import PromptBuilderService

CUSTOM_OVERRIDE = "You are a custom assistant. Always cite documents as [S1], [S2]."


def _build_system(citation_mode):
    svc = PromptBuilderService()
    messages = svc.build_messages(
        user_input="q",
        chat_history=[],
        chunks=[],
        memories=[],
        relevance_hint=None,
        system_prompt_override=CUSTOM_OVERRIDE,
        citation_mode=citation_mode,
    )
    return messages[0]["content"]


def test_disabled_mode_changes_custom_override_prompt():
    sys_default = _build_system(None)
    sys_disabled = _build_system("disabled")

    differs = sys_disabled != sys_default
    assert differs is True
