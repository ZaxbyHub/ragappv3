# fix(wiki): recompiling an edited document no longer leaves old claims active (#794)

## What changed

When a file is re-uploaded or rescanned, it keeps its `file_id` and its
`parsed_text` is replaced in place; the ingest wiki job then recompiles the
file against the NEW text. The deterministic claims pass only ever
find-or-creates claims for sentences present in the current text — nothing
superseded claims whose supporting sentence had disappeared, because
`WikiStore.mark_claims_stale_by_file` was wired only to the file-delete
paths. The removed sentence's claim stayed `status='active'` forever, and
wiki retrieval (which serves active claims only) kept surfacing it next to
the new, contradictory claim.

- **Recompile now retires un-re-derived claims.**
  `WikiCompiler.compile_ingest_job` calls the new
  `WikiStore.supersede_unrederived_file_claims` after the deterministic
  pass. Candidates are claims with a document source for this file, bound to
  this file's page, in this vault, currently `active`. A claim is kept when
  it was re-derived this run (including normalized-text reuse), or when any
  of its document-source quotes for this file still appears in the current
  text (exact, then the same normalization behind `find_claim_by_normalized_text`).
  A claim with no non-empty quote is undecidable and fails open — absence of
  evidence never supersedes.
- **Page scoping protects query-synthesis claims.** Claims created by the
  chat-answer (`compile_query_job`) path carry no page binding and quote the
  synthesized answer sentence rather than a file span; they are never
  candidates, so even a byte-identical recompile changes nothing for them.
  Curator claims keep their verbatim `source_quote`, so paraphrased
  `claim_text` survives recompiles while its quote exists.
- **Delete-path policy split applies.** Sole-source claims are set
  `superseded` plus one `stale` lint finding; claims with other INDEPENDENT
  sources stay active with one `weak_provenance` finding (the delete-path
  rule). Independent support means another file, a memory, or a non-echo
  manual source — a chat answer's `chat_synthesis` usage row attached to an
  ingest claim is a usage echo, not provenance, and does not keep a stale
  document claim co-serving next to its replacement. Findings use per-claim
  titles keyed on `(vault_id, finding_type, title)` and are inserted only
  when no row with that fingerprint exists in ANY status: a user's
  resolved/dismissed/acknowledged disposition permanently suppresses the
  fingerprint, and reactivating a claim deletes its OPEN stale finding, so a
  supersede→reactivate→supersede flap produces a fresh finding per genuine
  stale event without resurrection or orphans.
- **Quote presence is token-boundary and unescape-aware.** Presence is a
  word-delimited containment (padded on both sides), so a quote ending
  "Director" no longer matches text reading "Directorate" (partial-word
  matches kept stale claims alive), and a quote stored XML-escaped by the
  curator (its `&amp;` echo form) is unescaped before matching, so
  byte-identical recompiles keep curator claims whose raw text is unchanged.
  A quote that normalizes to the empty string (punctuation-only) and
  whitespace-only current text are undecidable and fail open.
- **Docstring corrected.** `mark_claims_stale_by_file` (and the memory
  sibling) no longer promise `status='stale'` — a value the `wiki_claims`
  CHECK forbids; the code writes `superseded`, and now the docstrings say so.
- **Recompiles report what they retired.** The compile result gains
  `claims_superseded` / `claims_weak_provenance` counts (surfaced in the
  compile job's `result_json`), and recompile lint findings now bind
  `related_page_ids_json` so they appear on the page detail and count toward
  the document's lint total.

New regression coverage: `backend/tests/test_o04_wiki_recompile_supersede.py`
(frozen acceptance checks: compile v1 Alice/Carol → recompile v2 Bob/Carol
leaves `(alice_active, carol_active) == (0, 1)`, and the docstring pin) plus
`backend/tests/test_o04b_recompile_supersede_policy.py` (11-test policy
battery: multi-source split + single-finding bounds, quote-presence keeps for
extraction-skipped and case-variant sentences — the case-variant fixture is
non-re-derivable so the normalized arm is exercised directly,
byte-identical recompile no-op, query-shape and curator-paraphrase
preservation, empty-text no-wipe, non-active and cross-vault untouched,
empty-quote fail-open) and `backend/tests/test_o04c_feedback_pins.py`
(feedback-round pins: finding lifecycle across a supersede→reactivate flap,
disposition suppression, chat-echo exclusion vs genuine manual support,
escaped-quote presence, token-boundary presence, whitespace-only text,
retirement result keys, exact compiler-created claim transition, memory-split
coverage, keep-set discriminating pin, finding row shape with a mixed
sole/multi-source recompile, fail-open variants, the no-commit reactivation
helper, and a curator-enabled integration test proving an accepted candidate
re-emitting a just-superseded claim reactivates it instead of burying it).

Residual (disclosed): the fix's coverage equals the compile job's coverage —
if the wiki ingest job is never enqueued (wiki compile flags off) or dies
before this step, staleness persists until the next successful compile, same
as the delete-path invalidation hook. Claims with a foreign/NULL page binding
whose only sources are files — including file sources acquired later through
text-reuse, and mixed memory-plus-file or chat-plus-file claims bound to
another page — are out of reach of the page-scoped step (the price of
excluding query-synthesis claims). A multi-source claim that keeps its
`weak_provenance` advisory keeps that open row even when the dropped quote
later returns (the advisory reflects provenance weakness, not staleness);
reactivation-cleanup applies to `stale` findings only. Curator quotes accepted only by the fuzzy
(`partial_ratio`) arm can still be judged absent by the presence check on a
later recompile; a degraded-but-non-empty re-parse mass-retiring a file's
claims is not separately guarded (the empty-text case is). Manual sources
other than `chat_synthesis` (e.g. operator notes) still count as independent
support.
