# rewrite_followup skips blank user turns (#992)

## What changed

- `backend/app/services/query_transformer.py` — `QueryTransformer.rewrite_followup`'s
  history scan now skips blank / whitespace-only USER turns before the
  `is_followup_query` gate, mirroring the assistant-turn blank guard that #928
  shipped in PR #988 (`if not content.strip(): continue`). The two arms of the
  scan are symmetric again.
- Consequences, both covered by the new spec file
  `backend/tests/test_followup_user_turn_whitespace.py`:
  - A whitespace-only newest user turn no longer shadows an older non-blank
    user question; the rewrite anchors on the older question instead of
    embedding an empty `<user_query>` block.
  - A history whose user turns are ALL blank/whitespace-only now leaves
    `prior_user` unset, so the existing `if not prior_user` guard returns the
    documented `None` insufficient-history signal without issuing an LLM call
    (callers already fall back to the original query).
- No other branch of the scan changed; no caller-facing signature changed.

## Why

Issue #992 (follow-up from the PR #988 review round): the user-turn arm bound
any non-follow-up `str` content, and `is_followup_query("   ")` is `False`
(the follow-up regex requires an explicit phrase; the pronoun gates require
non-empty stripped text). A whitespace-only newest user turn — reachable via
API clients that append blank user placeholders (streaming clients opening a
turn, bots) — locked the scan, so the rewrite prompt was anchored on blank
content with the real earlier question absent, violating the documented
contract of rewriting "using the prior question/answer for missing subject
matter". The `if not prior_user:` rescue could not fire because `"   "` is
truthy. The browser composer trims input, so server-side behavior is the
correct fix point.

## Migration steps

None. Pure server-side behavior hardening; no configuration, schema, or API
contract changes. Deployments that never send blank user turns see identical
rewrites.

## Known caveats

- A user turn containing only whitespace is now treated as "no turn" for
  anchoring purposes; it is never used as rewrite context and never suppresses
  an older question. This is the intended #992 semantics.
- Preserving coverage (AC3) relies on the existing suites
  `backend/tests/test_c16_followup_prior_turns.py`,
  `backend/tests/test_sibling_service_xml_escape.py`, and
  `backend/tests/test_c16b_cache_shape_guards.py`, which pin follow-up-phrase
  skipping, `is_followup_query` gating, assistant-turn emptiness skipping
  (#928), and the whitespace-only-response `None` contract; all pass unchanged
  at the fix head.
- The c16 spec file is checkpoint-frozen by PR #988's manifest, so the #992
  acceptance checks live in the new file
  `backend/tests/test_followup_user_turn_whitespace.py` rather than edits to
  the frozen file.
