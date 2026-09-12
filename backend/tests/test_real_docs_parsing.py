"""Nightly-tier real-parser fixture tests (ENH-007 / legacy-10).

These tests ingest the committed binary fixtures under
``backend/tests/fixtures/real_docs/`` with the REAL unstructured parser stack
and assert extraction behavior per format. They SKIP with an explicit reason
wherever the real stack is not installed — the PR-gate CI
(``requirements-ci.txt``) and plain local runs.

Stub handling (load-bearing): ``backend/conftest.py`` installs an
unconditional synthetic ``unstructured`` graph into ``sys.modules`` for every
pytest run under ``backend/``, so a naive ``import unstructured`` binds the
stub even in the nightly where the real package IS installed. This module
therefore (a) probes for the real package by importing it with the stub
temporarily evicted, capturing the resulting module graph, and (b) swaps the
real graph back in for the duration of each test, restoring the stub graph
afterward — other test suites keep their stub semantics untouched. The
nightly workflow (``.github/workflows/nightly.yml``) installs
``backend/requirements.txt`` (which carries ``unstructured[all-docs]``) and
runs this file before the full suite.

Contracts asserted (parser behavior, not plumbing):

* ``sample_table.pdf`` — the 3x4 text table extracts: at least one chunk
  carrying table rows and at least six of the twelve known cell values.
* docx / xlsx / pptx / md — every fixture format yields at least one text
  chunk containing its expected content markers.
* ``scanned_page.pdf`` — an image-only page (a drawn image XObject, zero
  text-showing operators in the PDF) must surface ZERO text chunks: the
  parser reports empty content instead of inventing any.
"""

import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "real_docs"

# The 3x4 table every real_docs fixture carries (header + Widget/Gadget/Gizmo
# rows). Used to assert table-ish extraction without over-constraining how a
# parser groups cells into chunks.
TABLE_CELLS = (
    "Item",
    "Qty",
    "Price",
    "Widget",
    "4",
    "9.50",
    "Gadget",
    "2",
    "17.25",
    "Gizmo",
    "7",
    "3.10",
)


def _unstructured_graph() -> dict[str, object]:
    return {
        name: module
        for name, module in sys.modules.items()
        if name == "unstructured" or name.startswith("unstructured.")
    }


def _evict_unstructured() -> dict[str, object]:
    """Remove every unstructured entry from sys.modules; return what was there."""
    graph = _unstructured_graph()
    for name in graph:
        del sys.modules[name]
    return graph


@contextmanager
def _real_unstructured():
    """Yield the REAL ``(unstructured, partition)`` behind the conftest stub.

    Raises ImportError when only the stub (or nothing) is available. The
    conftest graph in place on entry is restored on exit.
    """
    conftest_graph = _evict_unstructured()
    try:
        import unstructured
        from unstructured.partition.auto import partition

        if not getattr(unstructured, "__file__", None):
            raise ModuleNotFoundError("only a synthetic test stub is present")
        yield unstructured, partition
    finally:
        _evict_unstructured()
        sys.modules.update(conftest_graph)


def _probe_real_unstructured() -> str | None:
    """Return None when the real parser stack is importable, else why not."""
    try:
        with _real_unstructured() as (_unstructured, _partition):
            return None
    except ImportError as exc:
        return (
            "the real unstructured parser stack is not installed "
            f"({type(exc).__name__}: {exc}); the real-parser fixture set runs "
            "in the nightly full-dependency tier (.github/workflows/nightly."
            "yml), not this environment"
        )


_SKIP_REASON = _probe_real_unstructured()
pytestmark = pytest.mark.skipif(
    _SKIP_REASON is not None, reason=_SKIP_REASON or "unstructured available"
)


@pytest.fixture
def real_partition():
    """Run the test body with the real unstructured graph swapped in."""
    with _real_unstructured() as (_unstructured, partition):
        yield partition


def _parse(partition, fixture_name: str, strategy: str = "fast") -> list[str]:
    """Partition one fixture and return its non-empty chunk texts.

    ``strategy="fast"`` is pinned for the PDFs on purpose: it is deterministic
    (no model downloads) and sufficient for the text-showing operators these
    fixtures carry, so the nightly stays a stable behavior check rather than
    a model-download lottery.
    """
    elements = partition(filename=str(FIXTURES / fixture_name), strategy=strategy)
    return [str(element).strip() for element in elements or [] if str(element).strip()]


def test_sample_table_pdf_extracts_table_text(real_partition):
    chunks = _parse(real_partition, "sample_table.pdf")
    print(f"sample_table.pdf: {len(chunks)} chunks: {chunks}")
    assert chunks, "sample_table.pdf must yield at least one text chunk"
    row_chunks = [
        chunk
        for chunk in chunks
        if any(name in chunk for name in ("Widget", "Gadget", "Gizmo"))
    ]
    assert row_chunks, "table row text must survive PDF extraction"
    found = {cell for cell in TABLE_CELLS if any(cell in chunk for chunk in chunks)}
    assert len(found) >= 6, (
        f"table-ish cells under-extracted: {len(found)}/{len(TABLE_CELLS)} "
        f"cells found -> {sorted(found)}"
    )


@pytest.mark.parametrize(
    ("fixture_name", "markers"),
    [
        # docx carries the full table incl. numeric cells plus the intro line.
        ("sample_table.docx", ("Sample inventory table", "Widget", "Qty", "9.50")),
        # xlsx numeric cells are stored as numbers, not strings; the string
        # columns are the reliable markers.
        ("sample_table.xlsx", ("Widget", "Qty")),
        # pptx renders each table row as one |-joined text run per slide.
        ("sample_table.pptx", ("Widget", "Gadget", "|")),
        ("markdown_notes.md", ("Engineering notes", "|")),
    ],
)
def test_each_fixture_format_yields_chunks_with_markers(real_partition, fixture_name, markers):
    chunks = _parse(real_partition, fixture_name)
    print(f"{fixture_name}: {len(chunks)} chunks: {[c[:60] for c in chunks]}")
    assert chunks, f"{fixture_name} must yield at least one text chunk"
    combined = "\n".join(chunks)
    missing = [marker for marker in markers if marker not in combined]
    assert not missing, f"{fixture_name} missing expected text markers: {missing}"


def test_scanned_image_only_pdf_surfaces_zero_text_chunks(real_partition):
    """An image-only page must surface empty text, not invented content.

    scanned_page.pdf draws a 1-bit image XObject and contains no text-showing
    operators (no Tj/TJ), so there is nothing legitimate to extract. The
    contract is parser honesty: zero chunks (or all-empty chunks) is the
    correct answer and must reach the caller visibly.
    """
    raw_elements = list(
        real_partition(filename=str(FIXTURES / "scanned_page.pdf"), strategy="fast")
        or []
    )
    texts = [str(element).strip() for element in raw_elements]
    nonempty = [text for text in texts if text]
    print(
        "NOTE: scanned_page.pdf is image-only (no text-showing operators); "
        f"parser surfaced {len(texts)} element(s), {len(nonempty)} with "
        "non-empty text — empty is the correct, expected answer"
    )
    assert not nonempty, (
        "an image-only page must surface EMPTY text, not invented content; "
        f"got: {nonempty!r}"
    )
