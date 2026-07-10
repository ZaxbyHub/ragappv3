# docs(skills): narrow test and QA directives for .claude and .agents mirror trees

## What changed

Applied three narrowed skill directives — originally proposed by the /swarm finalize
--skill-review session and reviewed/accepted by `lowtier_critic` — to the
`.claude/skills/` and `.agents/skills/` runner-loaded mirror trees (only).

- **`writing-tests/SKILL.md` (Policy summary)** — two new bullets:
  - **Dataclass-default audit (retry/retry-like fields).** When changing the
    default of a dataclass field observable by tests (e.g., `TaskItem.attempt`,
    `EnrichmentTaskItem.attempt`), search tests for assertions that depend on
    the old default and update them; add a regression test that would fail
    under the old default if the new default changes behavior. Explicitly
    disallows literal `grep attempt=N` because the term is overloaded
    (`failed_attempts`, `retry_count`, etc.); enumerate affected fields by
    full import path.
  - **Post-sleep shutdown re-check (retry/backoff paths).** When a retry path
    sleeps before requeuing work, the production code MUST check the shutdown
    flag both before and after the sleep (the post-sleep re-check is needed
    because `stop()` may fire during the wait). The directive cites the
    canonical existing example — `BackgroundProcessor._enrichment_worker_loop`
    re-check pattern (`backend/app/services/background_tasks.py:706-715`) —
    and identifies the gap in `_handle_failure` (`:816` check vs. `:830-833`
    sleep with no post-sleep re-check). Tests that exercise a retry path
    SHOULD stop the processor/worker mid-sleep and assert queued work is
    dropped after the sleep completes.
- **`qa-sweep/SKILL.md` (Phase 3 — Completeness Verification)** — one new
  bullet:
  - **Regression-test baseline verification.** For every new regression
    test, identify the pre-fix commit or baseline hash (issue body, plan
    acceptance criteria, or "fails on commit X" line). Run the test against
    that baseline to confirm it fails, then run it against the fix branch
    to confirm it passes. Use `HEAD~1` ONLY when the fix is a single commit
    immediately before the test; in multi-commit or merge-batch fixes,
    prefer the documented pre-fix SHA. Aligns with the existing
    `writing-tests` "Verify regression tests are non-vacuous" policy.

## Why

The directives were surfaced by the /swarm finalize --skill-review session
from a prior phase ("Skill and Knowledge Recommendations Implementation").
Capturing them in the two runner-loaded trees (`.claude` and `.agents`)
makes them discoverable to future Claude Code and Codex sessions in the
RAGAPPv3 repo and prevents regression of the same patterns.

The `.opencode/skills/{writing-tests,qa-sweep}/SKILL.md` files were
intentionally **not** edited in this PR; see "Known caveats" below.

## Migration steps

No migration required. All changes are additive — new bullets in the
"Policy (summary)" section of writing-tests and a new bullet in
"Phase 3 — Completeness Verification" of qa-sweep. No existing text
was removed or altered.

## Breaking changes

None.

## Known caveats

- **Tree scope reduced from three mirrors to two.** AGENTS.md says
  "mirror across all three trees", and the `.opencode/skills/` copy of
  each edited skill currently contains stale RAGAPPv3 content. The user
  explicitly authorized the reduced scope ("retarget") because the
  `.opencode/skills/` directory is the opencode-swarm plugin's
  internal area — the plugin skips it when injecting skills for
  Claude Code or Codex sessions, so editing it would deepen drift
  without serving the target runners. A separate Option 2 plan at
  `.swarm/plans/option-2-skill-organization.md` formalizes the fix
  via a frontmatter `audience:` tag, after which this caveat is
  resolved.
- **spec.md FR-001..FR-006 deferred.** The broader "Skill and
  Knowledge Recommendations Implementation" spec contains code-level
  items (TaskItem.attempt default, shutdown guard, retry-disciplines
  skill creation, LanceDB .to_sql docs, knowledge entries) that are
  out of scope for this docs-only PR. They are tracked in the spec
  and may be addressed in follow-up PRs.
- **QA gate ratchet.** The user-approved QA gate profile (test_engineer
  off, sast off, drift_check off) was forced ON by set_qa_gates'
  ratchet-tighter-only policy. Documentation of the divergence is in
  `.swarm/context.md`.
