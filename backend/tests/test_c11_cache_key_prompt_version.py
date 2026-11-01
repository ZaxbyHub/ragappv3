"""Acceptance check for issue #716 — prompt version in the transform cache key (C9).

NEW-SURFACE: ``app.services.query_transformer`` must export a module-level
``PROMPT_VERSION`` constant and fold it into ``QueryTransformer._make_cache_key``'s
hash pre-image, so a prompt change invalidates stale cached query rewrites
instead of serving transforms built by the previous prompt.

At base (665bbd09):
- the constant does not exist (ImportError at module import -> PROMPT_VERSION
  is None here), and
- ``_make_cache_key`` hashes only ``(model, type, query)`` (query_transformer.py
  ~L227-237),

so patching a PROMPT_VERSION yields the SAME key — RED with two equal hashes.
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from app.services.query_transformer import PROMPT_VERSION  # noqa: F401
except ImportError:
    PROMPT_VERSION = None

from app.services.query_transformer import QueryTransformer


def test_prompt_version_changes_cache_key():
    """C9 — a changed PROMPT_VERSION must change _make_cache_key's output.

    RED at base: the version is absent from the key pre-image, so both calls
    hash identical input -> ``assert 'query_transform:<md5>' == 'query_transform:<md5>'``.
    ``create=True`` keeps mock from erroring on the not-yet-existing module
    attribute at base, so the RED signature is the equal-hash assertion.
    """
    qt = QueryTransformer.__new__(QueryTransformer)
    base_key = qt._make_cache_key("m", "step_back", "q")
    patched_version = PROMPT_VERSION + "x" if PROMPT_VERSION else "v2"
    with patch(
        "app.services.query_transformer.PROMPT_VERSION", patched_version, create=True
    ):
        other_key = qt._make_cache_key("m", "step_back", "q")
    assert base_key != other_key
