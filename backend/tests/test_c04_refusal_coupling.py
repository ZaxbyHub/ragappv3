"""Issue #709 review nit pin: the prompt and the refusal detector share ONE
refusal sentence constant.

F5 wires ``CITATION_INSTRUCTION`` to prescribe the exact
``PROMPT_REFUSAL_SENTENCE`` defined in citation_validator, so the model's
instructed refusal wording can never drift away from the phrases
``_looks_factual`` recognizes. The frozen C7 check pins only the detector
half; this pin makes reverting the prompt-side coupling (back to the vague
"clearly state it is not available") a visible failure instead of a silent
drift re-opening the defect.
"""

from app.services.citation_validator import PROMPT_REFUSAL_SENTENCE
from app.services.prompt_builder import CITATION_INSTRUCTION


def test_prompt_prescribes_the_shared_refusal_sentence():
    assert PROMPT_REFUSAL_SENTENCE in CITATION_INSTRUCTION


def test_shared_sentence_is_recognized_as_a_refusal_phrase_source():
    # The detector's phrase list must keep covering the shared sentence's
    # key fragment ("not available in the provided context").
    from app.services.citation_validator import REFUSAL_PHRASES

    assert any(
        phrase in PROMPT_REFUSAL_SENTENCE.lower() for phrase in REFUSAL_PHRASES
    ), "REFUSAL_PHRASES no longer recognizes the prompt-prescribed refusal"
