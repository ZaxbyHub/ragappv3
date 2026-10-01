---
issue: 689
title: Chat UI stays usable and truthful (Send visible at laptop widths, evidence shows the clicked answer's sources, errors keep their real cause)
---

# Chat surface usable at audited widths and truthful about sources and errors

Workstream A PR 7 of 7 (audit remediation, frontier audit 2026-09-23).

## What changed

- **The composer toolbar wraps instead of clipping Send/Stop off-screen**
  (#689 / UI-R1-02). The single non-wrapping toolbar row (~900px of controls:
  mode toggle, temperature/retrieval/citation selects, date/tag/author filters)
  inside the `max-w-[760px]` container now wraps, the middle group shrinks
  first, and Send/Stop are `shrink-0` — the primary action stays inside the
  composer box at 1024x768 with default rails.
- **The chat column keeps a usable width at 768px** (UI-R1-03). The session
  rail now defaults open only at >=lg (1024px); between md and lg the expanded
  nav rail (240px) plus a 320px session rail left only ~208px for the chat.
  The rail stays user-togglable at every width; >=1024 defaults are unchanged.
- **The phone evidence sheet no longer covers the composer** (UI-R4-14). The
  below-lg bottom sheet is 45vh (was 75vh), and the chat column is padded by
  the same height while the sheet is open, so the composer rides above the
  non-modal sheet (PRODUCT-ENH-10 preserved: still no overlay, no focus trap;
  the future #771 snap-point work is unaffected).
- **The evidence pane lists the CLICKED message's sources** (T1-13-K-04).
  `useSourcesForSourceId` accepts the clicked message id (which
  AssistantMessage already stored) and prefers that message's sources when it
  cites the chunk; un-anchored (persisted/forked) selections keep the
  first-match fallback. Selector references stay stable (#616).
- **Pre-content stream failures keep their real cause** (T1-13-K-05). The
  persistence guard's generic "The model returned an empty response" text is
  now applied only when no error was already stamped — an admission/capacity,
  network, 5xx, or pre-content-interrupt cause survives to the UI. The turn
  is still never persisted (LIVE-01/PRR-001 unchanged).
- **Source cards activate on Space** as on Enter (T1-13-K-08).
- **Extracted-output ids are structural** (T1-13-K-09): `message.id` + block
  ordinal instead of a 20-char content prefix, so same-prefix code blocks no
  longer collide as React keys.
- **Switching to a synthesized (or id-less) source clears the context
  spinner** instead of leaving it stuck (T1-13-S2-07).
- **A second jump-to-answer highlight survives the first jump's 1.5s timer**
  (T1-13-S2-10): the timer is owned by a ref, cleared on re-jump and unmount.
- **A failed mermaid chunk load shows the error fallback instead of an
  eternal spinner** (TQ-sibling-batch-01-06): the dynamic import chain got
  the same rejection handler `mermaid.render` already had.
- **Follow-up chips are grammatical** (UI-R1-04): the user's leading
  imperative verb is stripped from the topic, and the topic cap cuts at a
  word boundary without an ellipsis — chips no longer read "…risks around
  Explain the vendor onboarding process f…?".
- **Render budget held on the hot path** (TQ-sibling-batch-05-05):
  AssistantMessage (per-message) and Composer subscribe via scoped selectors
  instead of whole-store destructures, so unrelated shell-store changes (e.g.
  a rail resize) no longer re-render every message and the composer.

## Verification

13 frozen acceptance checks (issue-tracer v3): C1-C12 DISCRIMINATING, all RED
at base d9b602d1 and GREEN at head (C2's base leg measured the 768px chat
column at exactly 208px; the head leg measures >=360px); C13 PRESERVING
(#616 selector stability) GREEN at both. Full frontend suite 2567 passed,
typecheck/lint clean, Playwright smoke + width-budget e2e green against the
production build.
