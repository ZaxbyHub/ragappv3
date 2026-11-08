# g06 contributing-env-path gate fails closed on a paragraph naming neither env file (#1062)

Follow-up from PR #1059 (IMP-1). Sibling of #1061. Source of the hardening
target: #738 AC15 ("CONTRIBUTING.md env step names `backend/.env`").

## What changed

- **`scripts/check_g06_setup_docs.py` — `check_contributing_env_path` now
  requires the `**Environment:**` paragraph to name `backend/.env`
  unconditionally.** The FAIL previously required `.env.example` to appear in
  the paragraph first (`.env.example` present AND `backend/.env` absent), so a
  paragraph that named *neither* file — e.g. `**Environment:** install
  dependencies and run the backend.` — returned `[]` and the gate passed
  (probe-verified RED before this change). The condition collapses to
  `backend/.env not in paragraph_text`: the `.env.example`-without-
  `backend/.env` drift still fails exactly as before, and the names-neither
  drift now fails too. The missing-paragraph FAIL branch is unchanged, as is
  the pass branch for a paragraph naming both files.
- **`backend/tests/test_g06_gate_wiring.py` — new mode-level negative test
  `test_g06_contributing_env_path_requires_backend_env_name`** (synthetic
  repo texts via `_synthetic_repo_texts` + monkeypatched `read`, mirroring
  `test_g06_modes_fail_on_missing_documented_keys`). It pins the RED branch
  from this issue (paragraph naming neither file → FAIL list non-empty), the
  preserved `.env.example`-only FAIL branch, and the names-both pass.

## Why

- #1062 (PR #1059 post-merge swarm-critic adjudication, IMP-1): the gate could
  pass while CONTRIBUTING.md had silently lost the env-file pointer that
  #738 AC15 guarantees — a setup doc regression the mode was designed to catch
  would slip through whenever the paragraph mentioned no env file at all.
- The change is additive FAIL-branch only: every input that failed before
  still fails with the same message, and the only newly-failing inputs are
  paragraphs that name neither file. No frozen-check semantics change (final-
  critic adjudication, 2026-11-03).

## Migration steps

- None. The shipped CONTRIBUTING.md `**Environment:**` paragraph names both
  `.env.example` and `backend/.env`, so `python
  scripts/check_g06_setup_docs.py --check contributing-env-path` (and
  `--check all`) keep passing on the current tree — verified locally.
  Contributors whose local CONTRIBUTING.md drifts to an env paragraph without
  the `backend/.env` pointer will now see `FAIL contributing-env-path`.

## Known caveats

- The FAIL message for both in-paragraph drift shapes is the bare
  `FAIL contributing-env-path` (pre-existing shape, kept for stability; the
  missing-paragraph branch keeps its longer diagnostic). The mode does not
  pin the `.env.example` half of the documented copy instruction — a
  paragraph naming only `backend/.env` still passes, which matches #738
  AC15's guarantee as adjudicated in #1062 (out of scope there).
