---
issue: 868
title: Chat empty-state Suggested prompts use native ul/li; axe aria-required-children re-armed in the #778 gate
---

# Suggested prompts are a native list; the aria-required-children waiver is gone

Workstream L follow-up to #778 (a11y route matrix), issue #868.

## What changed

- **The chat empty-state suggested-prompts grid is a native `ul`/`li` list**
  (#868 AC2). `EmptyTranscript` in `frontend/src/components/chat/TranscriptPane.tsx`
  previously rendered `<div role="list" aria-label="Suggested prompts">` whose
  direct children were plain `<button>` elements — a critical axe-core
  `aria-required-children` violation (a `list` requires `listitem` children).
  It now renders `<ul aria-label="Suggested prompts">` with one `<li>` per
  prompt button; the CSS grid classes moved to the `<ul>` unchanged and each
  button fills its `li` grid cell, so the visual layout is identical.
- **The `aria-required-children` rule is re-armed in the frozen a11y gate**
  (#868 AC1, amendment 6 to the #778 checkpoint). The
  `"aria-required-children": { enabled: false }` waiver row is deleted from
  `frontend/e2e/a11y-matrix.spec.ts`, so axe runs the rule again across all
  23 routes x 320x568/640x360 x light/dark. The three waivers owned by the
  still-open #865/#866/#867 stay (each of those issues' AC1 re-enables its
  own rule).
- **`scripts/check_l07_a11y_gate_wired.py` pins the waiver set at exactly the
  three remaining rules** (`aria-valid-attr-value` #865, `nested-interactive`
  #866, `button-name` #867). Any fourth `enabled: false` row — including a
  re-waiver of `aria-required-children` — now fails the Quality contracts
  job, so the #868 re-arm cannot be silently reverted.
  `backend/tests/test_l07_gate_wiring.py` gained a mutant pin for exactly
  that re-waiver (`test_rewaived_aria_required_children_rejected`).
- **`docs/engineering/testing.md`** a11y-matrix job description updated to
  the three-waiver reality.

## Why

Full matrix run 2 of the #778 gate (2026-10-09) found the rule critical on
`/chat` and `/chat/:sessionId` empty transcripts in every viewport x theme
combination; amendment 5 waived the rule with this issue as owner. Screen
readers cannot compute list item count/structure from a `role="list"` whose
children are not listitems. The fix restores correct list semantics at the
source and re-arms the gate so the violation class cannot regress silently.

## Migration steps

None. Pure frontend markup change (same visible UI, same click behavior);
no API, storage, or configuration surface is touched.

## Known caveats

- The suggested-prompts `li` wrappers are plain `<li>` grid items; buttons
  gained `h-full w-full` to fill their cell (previously the buttons
  themselves were the grid items). No visual delta is expected, but the
  DOM under `ul[aria-label="Suggested prompts"]` changed shape — selectors
  that targeted the old `div[role="list"] > button` structure (none exist in
  this repo's tests; the jsdom and e2e suites query by role/text) would need
  updating.
- Sibling waived rules #865/#866/#867 remain disabled in the gate and are
  owned by their open issues; a collated re-arm PR for all four
  (ZaxbyHub/ragappv3#1022) is open but unmerged — this change lands the
  #868 slice independently, and the amended gate stays green with the other
  three waivers in place.
- The jsdom regression test exports `SUGGESTED_PROMPTS` from
  `TranscriptPane.tsx` (test-only export; not part of any public API).
