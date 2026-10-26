# feat(chat): RightPane evidence tabs surface the citation support chips on side-pane wiki/KMS cards (#965)

## What changed

The RightPane's Wiki and Knowledge (KMS) evidence tabs now render the same
"Support NN%" chip the assistant message has shown since #714:

- `frontend/src/stores/useChatStore.ts` gains
  `useLastCompletedAssistantCitationConfidence`, a granular selector mirroring
  the existing `useLastCompletedAssistantSources` / `…WikiRefs` / `…KmsRefs`
  scan-back pattern (skip the streaming message id, `useShallow`): it returns
  the `citationConfidence` record of the most recent *completed* assistant
  message whenever it carries the field at all (an explicitly empty record
  counts and stops the scan, so an older message's scores are never paired
  with a newer message's refs), and undefined only when no completed message
  carries the field.
- `frontend/src/components/chat/RightPane.tsx` passes that record as the
  optional `citationConfidence` prop to its two card call sites
  (`<WikiCards wikiRefs={…}>` in the wiki tab and `<KMSCards kmsRefs={…}>` in
  the KMS tab). Both card components already accepted the optional prop
  (#714) and compute `supportScore={citationConfidence?.[label]}`; the pane
  was the only remaining surface that omitted it, so cited side-pane cards
  never showed a support indicator.
- Tests: new `frontend/src/components/chat/RightPane.c09-support.test.tsx`
  (real store + real card components; pins the wiki chip, the "Support 0%"
  KMS chip, the title-attribute-only disclaimer, the never-"verified" rule,
  and the absent/empty-score no-chip case) and four selector unit tests in
  `frontend/src/stores/useChatStore.selectors.test.ts` (last-completed scan,
  streaming-skip, empty-record stop, undefined-when-absent).

## Why

Issue #965 (follow-up to #714): the RightPane renders the same
`WikiCards`/`KMSCards` components as the assistant message but without the
`citationConfidence` prop, so a cited wiki or KMS card in the side pane showed
no `Support NN%` chip even when the done payload carried a score for its
label. The #714 chip contract is reused verbatim — visible text
`Support NN%`, the lexical-measure wording only in the `title` attribute,
never the word "verified" as a status claim — and no card component changed.

## Migration steps

None. UI-only and backward-safe: the prop is optional, so messages without
scores keep the byte-identical no-chip rendering (AC3). Behavior changes only
when a completed assistant message actually carries `citationConfidence` from
the #714 done payload.

## Known caveats

- The pane's evidence slices stay independent (wiki refs, KMS refs and the
  confidence record are each sourced from the most recent completed assistant
  message carrying that slice). In the normal flow all three land on the same
  done message; in mixed histories the empty-record scan-stop prevents stale
  scores from flowing forward, but a newest-refs / older-scores split could
  still pair across messages. The assistant-message surface pairs by
  construction and is unaffected.
- MemoryCards in the pane are untouched — the pane has no memories tab today
  (out of scope per the issue). MarkdownMessage inline chips are likewise out
  of the citation-card contract (#714 scope).
