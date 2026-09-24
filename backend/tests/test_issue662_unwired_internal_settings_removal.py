"""Removal guard for issue #662 (dormant internal settings).

``recency_decay_lambda``, ``retrieval_profile``, ``sparse_embedding_timeout``,
and ``sparse_search_max_candidates`` were declared on ``Settings`` but never
read anywhere in production code (issue #662's census; the same four were
deliberately left behind by the #614/#625 cleanup as "internal-only" knobs).
They were removed outright, each with verified evidence that no wire-able
consumer path exists:

- ``sparse_search_max_candidates`` / ``sparse_embedding_timeout`` belonged to
  the learned-sparse/FlagEmbedding search path removed by the Harrier
  migration; the only remaining sparse surface is the schema-compat column.
- ``recency_decay_lambda`` was the exponential-decay parameter of a formula
  never implemented — the wired ``retrieval_recency_weight`` blends recency
  with linear min-max normalization.
- ``retrieval_profile`` was superseded by its own independently-wired boolean
  (``chunk_enrichment_enabled``); no code path ever branched on the string.

This guard pins the removal so the fields cannot be silently reintroduced
without a wiring decision: a future Settings field must be declared AND
consumed, or carry a reasoned entry in the dormant allowlist enforced by
scripts/check_settings_consumers.py.
"""

from app.config import Settings

REMOVED_FIELDS = (
    "recency_decay_lambda",
    "retrieval_profile",
    "sparse_embedding_timeout",
    "sparse_search_max_candidates",
)


def test_issue662_removed_fields_absent_from_model_fields():
    model_fields = Settings.model_fields
    for field in REMOVED_FIELDS:
        assert field not in model_fields, (
            f"Settings.{field} was removed as unwired (issue #662); "
            "reintroduce it only together with a production consumer "
            "(scripts/check_settings_consumers.py enforces this) or a "
            "reasoned allowlist entry, not as a declaration-only field"
        )


def test_issue662_removed_fields_absent_from_settings_instance():
    settings_obj = Settings()
    for field in REMOVED_FIELDS:
        assert not hasattr(settings_obj, field), (
            f"Settings instance should not have attribute {field} - "
            "it was removed as unwired (issue #662)"
        )
