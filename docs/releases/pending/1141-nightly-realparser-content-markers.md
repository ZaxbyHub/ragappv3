# fix(ci): nightly real-parser fixture markers assert content, not serialization (#1141)

## What changed

`backend/tests/test_real_docs_parsing.py`'s `markdown_notes.md` parametrize row
asserted the literal pipe glyph `|` as a text marker. The real full-deps
markdown pipeline (unstructured 0.18.32 `partition_md` → python-markdown
`tables` extension → HTML → `partition_html` → a `Table` element whose text
joins cells with spaces) reconstructs the table representation and never emits
the source delimiter glyph, so the marker was unsatisfiable in the only venue
that executes it — the nightly `full-deps` tier failed at the "Real-parser
fixture tests" step on every scheduled run since the tier began actually
running (2026-10-31), masking the vocabulary gate, the watchdog-wrapped full
suite, and the parser bake-off downstream. The row now asserts extracted
content cell strings: `("Engineering notes", "Widget", "9.50")`.

The pptx row's `"|"` marker is UNCHANGED and correct: the fixture's pipes are
literal slide-text content (proven by the slide XML text nodes; python-pptx
extracts them verbatim), not parser-generated join glyphs.

A new PR-gate guardrail, `backend/tests/test_real_docs_marker_philosophy.py`,
fails when a marker in that parametrize table stops being a defensible
assertion: fail-closed table integrity (non-literal rows, a dropped fixture
format, or a missing markdown row are failures, not skips), unconditional
rejection of empty or whitespace-only markers (vacuous membership passes), a
markdown-row rule keyed to the `.md`/`.markdown` extension (alphanumeric
content markers, no pipe glyph — a conservative fixture-scoped policy since
that fixture's pipes are all table delimiters), and a source-provenance rule
for other rows (bare-glyph markers are legal only when present verbatim in the
fixture's displayed content text: for Office formats the OOXML `t`/`v` content
nodes of a fixed set of document parts — not raw ZIP/markup, so declarations,
formulas, attributes and metadata cannot certify a glyph; plain-text formats
use decoded raw bytes; unknown/binary formats fail closed).

## Why

Issue #1141 (filed from the PR #1138 closeout): a chronically red nightly gate
is lost signal — the tier's isolation check and bake-off never ran, and the
#1021 watchdog wrapper had no live-run evidence. Format-pinning markers can
never be satisfied by a pipeline that reconstructs representation; content
markers pin the actual contract (table content survives extraction).

## Migration

No migration required. The test file skips in the PR gate (stubbed
unstructured); the marker change is exercised only in the nightly full-deps
tier. Post-merge, the first scheduled nightly run provides the AC evidence:
the Real-parser step green plus the first live "watchdog preflight" line in
the full-suite step log.
