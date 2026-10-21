# fix(rag): rewrite_followup skips empty prior assistant turns and returns None on whitespace-only rewrites (#928)

Follow-up to the PR #919 review round. Two pre-existing defects in
`QueryTransformer.rewrite_followup` (`backend/app/services/query_transformer.py`)
made the follow-up rewrite mishandle degenerate prior-turn shapes. All
changes are backend; no schema migration; no new settings; no frontend
change.

## What changed

- **An empty newest assistant message no longer suppresses older assistant
  turns.** The reversed prior-turn scan anchored `prior_assistant` on the
  first assistant entry whose content was a `str` — an empty string
  satisfied that check, so the scan stopped and every older non-empty
  assistant turn was discarded, sending the rewrite prompt out with an
  empty `<assistant_answer>` context block. Realistic trigger: a streaming
  turn that errored after opening the assistant message, or a client that
  appends an empty assistant placeholder. The assistant branch now skips
  empty/whitespace-only content and keeps scanning, mirroring the
  follow-up-phrase skip the adjacent user-turn branch already performed.
  History whose only assistant message is empty still rewrites from the
  prior user turn alone (`(prior_assistant or "")` already tolerated
  `None`); that path was green before and after and is frozen by a test.
- **A whitespace-only rewrite response returns `None` instead of raising
  `IndexError`.** The post-processing guard `if not response:` passes a
  whitespace-only string, and `response.strip().splitlines()[0]` then
  indexed an empty list, so the `IndexError` escaped the method —
  violating its documented return-`None`-on-failure contract (the
  surrounding `try/except` wraps only the `chat_completion` await). The
  guard is now `if not response or not response.strip():`. Everything
  after the guard — first-line selection, surrounding quote/bracket
  stripping, minimum-length rejection — is byte-identical and frozen by a
  preserving test.

Tests: `backend/tests/test_c26_followup_prior_turn.py` (recording-LLM
harness of `test_c06_followup_escape.py`) freezes all four acceptance
criteria — the two discriminating checks are RED at the #928 base and
GREEN at the fix head; the two preserving checks (empty-only-assistant
history still rewrites; non-empty post-processing unchanged) are GREEN at
both. The sibling escape suites
(`backend/tests/test_sibling_service_xml_escape.py`,
`backend/tests/test_c06_followup_escape.py`) stay green.

## Why

The method's contract is to return `None` when it cannot produce a usable
rewrite so the caller falls back to the original query; raising past that
contract and silently dropping prior-answer context both degrade follow-up
retrieval quality on histories that real clients produce (error
placeholders, empty streaming stubs). Both defects were found adjacent to
the #711 rewrite work during the PR #919 review and filed as #928.

## Migration steps

None. No settings, schema, or API changes; the behavior change is internal
to the follow-up rewrite path. Deployments that previously logged a caught
`IndexError` from the sole caller (`RAGEngine`, which wraps the call in a
`try`) will now see the documented `None` fallback instead.

## Known caveats

- The assistant-turn skip treats whitespace-only assistant content
  (e.g. a bare `"\n"`) as empty, same as `""`. An assistant turn that is
  deliberately whitespace-only carries no retrieval signal, so skipping it
  matches the intent; no whitelist exists for whitespace-only answers.
- The prior-user scan is unchanged: a history whose every user turn is a
  follow-up phrase (`is_followup_query`) still returns `None` by design
  (nothing to anchor on) — out of scope for this issue (#919 review
  follow-up family: #918, #922, #924).
