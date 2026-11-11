"""Follow-up #1073 merge-insert delta-scan probe (issue #1076 AC6 / #1075 sibling).

``VectorStore.add_chunks`` rewrites a COMPLETE same-generation re-embed in
place via

    table.merge_insert(on="id")
        .when_matched_update_all()
        .when_not_matched_insert_all()
        .execute(processed_records)

(backend/app/services/vector_store.py ~1447-1451, the PR #828 path). The
rewritten rows land in the table's un-indexed delta fragment, and every
later ANN ``search()`` must still see them — the whole suite is mock-table
based, so this assumption is unpinned. A real-LanceDB-table probe is the
only honest way to pin it.

This module does NOT stub lancedb: it runs only where the real package is
importable and skips otherwise (``pytest.importorskip``).

This probe is GREEN at base and after the fix — it pins an environmental
assumption, not a defect. It goes RED if LanceDB ever stops scanning the
delta fragment a merge_insert produces (e.g. an index/compaction behavior
change), which would make same-generation re-embeds silently serve stale
vectors under the new identity.
"""

import random

import pytest

pytest.importorskip("lancedb")

import lancedb  # noqa: E402
from lancedb.index import IvfPq  # noqa: E402

# Distinctive replacement vector: uniform 0.9 components — maximally far
# from the random seed rows' direction, so finding it is the index's doing.
_NEW_VECTOR = [0.9] * 8

_REWRITTEN_TEXT = "REWRITTEN: same-generation re-embed replacement text"
_INSERTED_TEXT = "INSERTED: brand-new row from the same merge_insert"


@pytest.mark.asyncio
async def test_merge_insert_rewritten_rows_stay_visible_to_ann_search(tmp_path):
    """Rewritten + newly inserted merge_insert rows must be ANN-findable.

    ~300 rows of random dim-8 vectors (above the 256-row index threshold
    the production path gates on), an IvfPq index with parameters valid for
    that row count (num_partitions=4; num_sub_vectors=2 divides dim 8),
    then ONE merge_insert that (a) rewrites an EXISTING id with the
    distinctive vector and new text and (b) inserts a brand-new id carrying
    the same vector — exactly the shape vector_store.py ~1447-1451 emits
    for a complete same-generation re-add.
    """
    rng = random.Random(1075)
    dim = 8
    rows = [
        {
            "id": f"row-{i}",
            "text": f"original text {i}",
            "vector": [rng.random() for _ in range(dim)],
        }
        for i in range(300)
    ]

    db = await lancedb.connect_async(str(tmp_path / "lancedb"))
    table = await db.create_table("documents", data=rows, mode="overwrite")
    await table.create_index(
        column="vector",
        config=IvfPq(
            distance_type="cosine", num_partitions=4, num_sub_vectors=2
        ),
        replace=True,
    )

    await (
        table.merge_insert(on="id")
        .when_matched_update_all()
        .when_not_matched_insert_all()
        .execute(
            [
                {
                    "id": "row-5",
                    "text": _REWRITTEN_TEXT,
                    "vector": _NEW_VECTOR,
                },
                {
                    "id": "brand-new-1",
                    "text": _INSERTED_TEXT,
                    "vector": _NEW_VECTOR,
                },
            ]
        )
    )
    assert await table.count_rows() == 301

    # Ground truth first: the merge_insert really updated / inserted the
    # rows (a scan-level read, no ANN involved) — so a later ANN miss is a
    # delta-visibility failure, not a failed write.
    stored = (
        await table.query()
        .where("id = 'row-5' OR id = 'brand-new-1'")
        .select(["id", "text"])
        .to_list()
    )
    stored_texts = {row["id"]: row["text"] for row in stored}
    assert stored_texts.get("row-5") == _REWRITTEN_TEXT, (
        "merge_insert did not rewrite the existing id: scan-level read "
        f"returned {stored_texts!r}"
    )
    assert stored_texts.get("brand-new-1") == _INSERTED_TEXT, (
        "merge_insert did not insert the brand-new id: scan-level read "
        f"returned {stored_texts!r}"
    )

    # The assumption under test: the ANN query near the NEW content finds
    # both rows, the rewritten one carrying its NEW text.
    query = await table.search(_NEW_VECTOR)
    results = await query.limit(5).to_list()
    hits = {row["id"]: row["text"] for row in results}

    assert hits.get("row-5") == _REWRITTEN_TEXT, (
        "merge_insert delta-scan assumption failed: the ANN query near the "
        "rewritten vector did not return the rewritten row with its NEW "
        f"text (top-5 hits: {hits!r}); same-generation re-embeds would "
        "silently serve stale content under the new identity"
    )
    assert hits.get("brand-new-1") == _INSERTED_TEXT, (
        "merge_insert delta-scan assumption failed: the ANN query near the "
        "new vector did not return the row inserted by the same "
        f"merge_insert (top-5 hits: {hits!r})"
    )
