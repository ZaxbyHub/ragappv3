# Test-integrity tail: fake fidelity + guardrail blind spots (#745, #746, #747)

Collated Workstream H closeout (PRs 8+9+10 of 10). Test/gate-only change:
no runtime code changed except `scripts/check_installation_doc.py` (DOC-003b
now evaluates commented `# CHAT_MODEL=` example lines and fails on a vacuous
empty evaluation — INSTALLATION.md gains the `ollama pull minicpm5-2b` line
so the real doc passes).

- **#745 (test doubles vs real contracts):** new
  `backend/tests/test_h08_fake_fidelity.py` (12 frozen checks). Realistic
  768-d identical-embedding case with a deterministic raw-cosine->1.0
  overshoot precondition; IMAP failure-path fake whose `logout()` raises
  `aioimaplib.Abort` (asserting -1 under `xfail(strict)` linked to #797);
  slow-socket `check_chat`/`_probe_client` timeout + `max_tokens=1` probes
  (two named mutants now die); c13 oracle derives the Qwen3 auto doc prefix
  through `EmbeddingService.embedding_doc_prefix`; a REAL
  `unstructured.documents.elements` vocabulary leg (conftest-stub eviction +
  restoration; nightly full-deps runs it for real, with a new no-skip gate
  step proving the venue); classification-aware RAGTrace seeding (7 redacted
  fields get SECRET markers asserted absent, 13 log-safe fields asserted to
  survive); MemoryPage edit flow asserts the EXACT `updateMemory` payload
  (trim, explicit-null clears, `expected_updated_at`). The 12 retrieval
  fixtures now key relevance `_score` (real LanceDB FTS key); 3
  outcome-flip assertions are `xfail(strict)` linked to #1070 (the product
  reads the legacy `score` key — filed separately). Pins: get_pool doubles
  keep the exact real signature; fallback-chunk fakes stay a subset of the
  engine union; every reranking `.predict` call keeps the #716
  identity-activation contract.
- **#746 (guards that cannot detect their target):** new
  `backend/tests/test_h09_blind_spots.py` (7 frozen checks). Restore drill
  drives the documented harness with the previously-missing no-draft-room
  and empty-vault inputs (pins of the fixed product); the #652 timeout
  census catches positional, attribute-qualified, and signed numeric
  timeouts with safe negative controls; the coverage-design pin parses
  `vite.config.ts` with the REAL TypeScript compiler (#624 pattern) so a
  commented-out include entry or threshold fails; the win32 skip on the seq
  concurrency test is removed (crash rationale disproven twice on a real
  Windows host); CONTRIBUTING documents the python-magic/msys-magic-1.dll
  collection hang and the PATH workaround.
- **#747 (verify-and-close):** the settings-consumer census gate verified
  green and wired (ci.yml + justfile + the 11 gate contract tests + the
  #614 removal pin); no code change.

Follow-ups filed: #1070 (`_score` product read), the TEST_SCHEMA
fixture-fidelity conversion issue (see PR body), ABSORB comment to #797
(IMAP Abort ownership).
