"""Issue #709 acceptance check (C04, AC7): a refusal worded like the
default prompt's own instructed refusal must not be flagged as
unverifiable claims.

The default system prompt (``prompt_builder``) instructs the model:
"If no context supports the answer, clearly state it is not available."
A compliant refusal therefore reads like "This information is not
available in the provided context." — but ``_looks_factual`` in
``citation_validator`` only recognizes four other refusal phrasings
("not available in the retrieved", "i don't have enough", "no relevant",
"the retrieved documents"). The prompt-instructed wording slips past the
refusal filter, so every sentence of a CORRECT refusal gets listed in
``unverifiable_claims`` (false-positive hallucination flags on the exact
behavior the prompt asked for).

Contract: scoring a prompt-worded refusal against unrelated sources must
yield zero unverifiable claims. Pure check — no DB, no HTTP.
"""

from app.services.citation_validator import score_citations

REFUSAL_CONTENT = (
    "This information is not available in the provided context. "
    "I cannot answer the question from these sources. "
    "Please upload a relevant document."
)


def test_prompt_worded_refusal_has_no_unverifiable_claims():
    result = score_citations(
        REFUSAL_CONTENT,
        source_texts=[
            "Unrelated passage about quarterly widget shipments in Norway."
        ],
    )
    assert len(result.unverifiable_claims) == 0
