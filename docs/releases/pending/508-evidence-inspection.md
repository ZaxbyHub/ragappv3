# feat(chat): precise, persistent and accessible source inspection (#508)

## What changed

Chat source inspection (Workstream A2, issue #508):

- **Fixed owned audit findings**
  - UI-025: KMS reference cards now open through the configured deployment basename
    (`appPath`), so `/meridian` deployments no longer leak `/kms/...` links outside the app mount
    (`KMSCards.tsx`).
  - UI-028: `WikiEvidence.to_dict()` now serializes `excerpt`; page-only wiki references keep
    their summary body through SSE, persistence and reload (frontend already consumed it).
  - UI-034: image artifact blob URLs are revoked exactly once on unmount (StrictMode-safe);
    unmount during an in-flight artifact fetch creates no URL.
  - UI-035: a failed artifact image fetch no longer hides the expanded text fallback — the error
    notice renders above the text.
  - UI-036: search terms containing regex metacharacters (`C++`, `config.json`) now highlight.
  - UI-038: "Jump to answer" carries the originating message id end-to-end (selection → dispatch →
    transcript), so a source reused by a later answer jumps to the answer you came from; without an
    anchor (reload/fork/legacy flows) the previous first-match behavior applies.
  - UI-046: evidence cards are derived from the same markdown-aware traversal as inline citation
    chips — markers inside fenced/inline code no longer create evidence cards (`remark-parse` and
    `unified` are now explicit frontend dependencies; they were already in the bundle).
- **Retrieved vs cited separation (CHAT-UX-04, #229 legacy-07b)**
  - The retrieval pipeline now emits a versioned `evidence` SSE event (`version: 1`,
    `phase: "candidates"`) as soon as retrieval (+ vision enrichment) finishes, reusing the exact
    `done.sources` serialization — no second label namespace.
  - While streaming, the evidence panel lists candidates under "Retrieved — not yet cited"; the
    finalized `done` payload replaces them. Candidates never render a cited/verified badge.
  - Final source cards show an honest Cited/Retrieved badge derived from the answer's validated
    citation labels. Non-stream chat responses never emit candidates (by design).
  - The agentic retrieval path (multi-round tool loop, owned by B1/CITE-002) does not emit
    candidate events yet.
- **Honest confidence presentation (CHAT-UX-05)**
  - The per-citation Jaccard score is now labeled "N% textual overlap" with an explanation that it
    is a lexical measure — not a probability of correctness — and distinct from retrieval
    relevance. No probability labels were introduced (F2 owns calibration).
- **Support-span grounding (CHAT-UX-02)**
  - The expanded-context preview highlights the exact supporting passage (literal, whitespace-
    normalized match of the source snippet) with a distinct support-span style; when the passage
    is not present in the expanded context (parent-window text), an explicit note says so — this
    is expected behavior, not an error. Term search remains a separate explicit control.
- **Honest unavailability + evidence contract (PRODUCT-ENH-02/03 consumer behavior)**
  - New `frontend/src/lib/evidence.ts` contract (`EvidenceView`, location, availability,
    cited-state) consumed by the preview header and source badges; Draft Room adopts it in D3.
  - Context fetch failure keeps the saved excerpt visible with "original context unavailable" and
    a working Retry (refetch is source-scoped and abort-safe). Document original-fetch failure
    keeps the excerpt. Location renders page/section/artifact description or "Location
    unavailable" honestly.
  - Files-table revision identity (file_hash/ingestion_version) is NOT on the wire: chunk
    metadata does not carry it at retrieval time and adding it would require a files-table join in
    the retrieval hot path (B1-adjacent). Recorded deliberately; not silently deferred.
- **Responsive drawer + keyboard access (PRODUCT-ENH-10)**
  - Below `lg`, the evidence drawer is non-modal (no overlay, no focus trap): the composer and
    transcript stay interactive while evidence is open. Escape closes the evidence pane on desktop
    and drawer; focus returns to the originating citation chip (or the pane toggle). Switching
    sessions or starting a new chat resets evidence selection (clearing messages within a session
    intentionally keeps it).
- **PDF**: page targeting verified (`source.page_number` → viewer; no page → no fragment).

## Why

Issue #508 requires chat evidence to be precise (exact passage, punctuation-safe highlighting,
code-aware citations), persistent (excerpts survive transport/storage/reload), honest (retrieved ≠
cited ≠ verified; overlap ≠ confidence) and accessible (keyboard open/close/back, focus
restoration, responsive drawers that leave the composer usable).

## Migration steps

- None required. Backend SSE gains one additive event type; parsers ignore unknown events, so old
  frontends paired with the new backend simply never see the candidates surface. New frontend +
  old backend degrades gracefully (no candidates section). Stored messages tolerate the new
  optional JSON keys (`excerpt` in `wiki_refs`).

## Breaking changes

- None. `done` payload shape, REST contracts, and DB schema are unchanged.
- Behavior change by design: citation confidence wording changed from "N% confidence" to
  "N% textual overlap" (tests asserting the old wording were updated deliberately).

## Known caveats

- Candidates arrive during the "Reading" stage (retrieval completes before prompt assembly; the
  event fires after vision enrichment so it matches final serialization exactly).
- Agentic (multi-round) retrieval does not emit candidate events yet (B1 owns that path's label
  contract).
- Cross-browser PDF/viewport verification (390/768/1126/wide, PDF iframe) was covered by jsdom +
  code-level checks in CI; a live browser matrix remains a manual gate.
- Support-span highlighting is a literal match; distilled or parent-window contexts that do not
  contain the snippet verbatim show the explicit "not located" note.
- Evidence selection state intentionally persists across an in-session "clear messages".
