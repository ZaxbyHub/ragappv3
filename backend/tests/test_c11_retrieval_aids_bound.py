"""Acceptance checks for issue #716 — retrieval-aids bound handling (C5/C6).

C5: ``parse_derived_response`` must TRUNCATE an over-bound ``retrieval_aids``
list to ``MAX_AIDS`` instead of rejecting the whole provider response. At base
(665bbd09) the parser raises ``DerivedError("retrieval_aids must be a bounded
list")`` for 9 aids (multimodal_prompts.py ~L262-263), so a valid description
is discarded wholesale and the artifact stays unsearchable.

C6: ``build_system_prompt`` must state the aid limit as a standalone number so
the model knows the bound before emitting the JSON list.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import multimodal_prompts as mp


def test_nine_aids_truncated_to_eight():
    """C5 — 9 aids must truncate to exactly MAX_AIDS (8), not raise DerivedError.

    RED at base: parse_derived_response raises DerivedError for a 9-element
    list, so n stays -1 -> ``assert -1 == 8``.
    """
    payload = {
        "description": "A quarterly revenue chart for the EMEA region.",
        "retrieval_aids": [f"searchable aid {i}" for i in range(9)],
    }
    raw = json.dumps(payload)
    result = None
    try:
        result = mp.parse_derived_response(raw, "v1")
    except mp.DerivedError:
        result = None
    n = len(result["retrieval_aids"]) if isinstance(result, dict) else -1
    assert n == 8


def test_prompt_states_aid_limit():
    """C6 — the system prompt must contain MAX_AIDS as a standalone number.

    RED at base: build_system_prompt() carries no digit 8 anywhere, so the
    regex finds nothing -> ``assert False``.
    """
    assert bool(re.search(rf"\b{mp.MAX_AIDS}\b", mp.build_system_prompt()))
