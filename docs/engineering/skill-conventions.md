# Skill Conventions

Canonical specification for the repo's agent-runner skills across the three
runner trees (`.claude/skills/`, `.agents/skills/`, `.opencode/skills/`).
Referenced by `AGENTS.md` and `docs/engineering/conventions.md`; enforced by
`backend/tests/test_skill_tree_collapse.py`. This document is the source of
truth for:

- What counts as a repo-specific vs a runner-specific, framework-vendored, adapter, or generated skill
- The canonical home of every skill (one canonical tree per skill; no full-copy mirrors)
- The thin-pointer adapter pattern that keeps multi-runner discoverability without duplication
- Frontmatter shape
- The `.secretscanignore` validation contract
- AC traceability format

## Scope categories

Every skill directory under any of the three trees falls into exactly one
category. The category determines the skill's canonical home.

| Category | Canonical home | Examples |
|---|---|---|
| **Repo-specific** | `.agents/skills/` (thin `.claude` pointer adapters) | `swarm`, `qa-sweep`, `engineering-conventions`, `commit-pr` |
| **Runner-specific** | the single tree of the runner that owns it | `.claude/`: `coding-agent`, `ship`, `github`, `reviewing-*`; `.agents/`: `contributing`, `subprocess-safety`; `.opencode/`: `deep-research`, `loop`, `swarm-pr-subscribe` |
| **Framework-vendored** | `.claude/skills/` (vendored from the upstream swarm framework; reference deleted `.swarm/` and absent `src/agents/architect.ts`) | `brainstorm`, `clarify`, `plan`, `execute`, `council`, `critic-gate`, `phase-wrap`, `pre-phase-briefing`, `resume`, `specify`, `consult`, `deep-dive`, `design-docs`, `discover`, `issue-ingest`, `clarify-spec` |
| **Adapter** | one tree holds the canonical protocol; other trees hold thin pointers | `codebase-review-swarm` (canonical `.opencode/skills/codebase-review-swarm/`) |
| **Generated** | `.opencode/skills/generated/` (auto-generated knowledge skills; plugin-internal) | the 12 `generated/*` skills |

The current category membership and every invariant in this document are
pinned by `backend/tests/test_skill_tree_collapse.py`; editing a skill's
canonical home without updating that test (and this table) fails the Backend
CI job.

## Canonical homes

One canonical tree per skill — never a second full copy in another tree:

| Category | Canonical tree | Extra trees hold |
|---|---|---|
| Repo-specific (22 skills) | `.agents/skills/<name>/` | thin pointer `SKILL.md` in `.claude/skills/<name>/` |
| Framework-vendored (16 skills) | `.claude/skills/<name>/` | nothing (opencode discovers `.claude` natively) |
| Runner-specific (16 skills) | the owning runner's tree | nothing |
| `codebase-review-swarm` | `.opencode/skills/codebase-review-swarm/` | thin adapters in `.claude` and `.agents` |
| Generated (12 skills) | `.opencode/skills/generated/` | nothing (plugin-internal) |

Pruning tally (issue #569, 2026-09): the collapse deleted 38 `.opencode`
mirror copies (22 repo-specific + 16 framework-vendored) and converted the 22
`.claude` repo-specific full copies into thin pointers; `.opencode/skills/`
went from 43 top-level entries to the 5 enforced survivors (4 skill dirs plus `generated/`; the enforced inventory is the survivor set in `backend/tests/test_skill_tree_collapse.py`, not the prose count here). No skill content was retired in this
pass and no whole skill was deleted; deleted content remains recoverable from
git history.

**When you add or change a repo-specific skill:** create or edit it in
`.agents/skills/<name>/`, then add or update the thin pointer under
`.claude/skills/<name>/` (see the adapter pattern below). `phase-wrap`'s
canonical `.claude` copy carries two gotcha lines merged from the former
`.opencode` copy during the #569 collapse; it is framework-vendored, so its
canonical is `.claude` per the table above.

Historical note: before #569 the model was a byte-identical three-tree mirror
enforced by a dedicated local sync script plus a CI wrapper, with
canonical precedence `.agents > .claude > .opencode` and two per-skill
canonical overrides (`ci-fix-monitor` → `.opencode` for a force-push
addendum, `commit-pr` → `.claude` for runner-neutral wording). All mirrored
copies were byte-identical at collapse time and the overrides' content had
already propagated, so the precedence winner (`.agents`) became the canonical
home for every repo-specific skill with zero content change. That machinery
was removed; a drift-gate reintroduction would need a new justification.

## Frontmatter shape

Canonical (required):

```yaml
---
name: <skill-name>
description: <one-paragraph prose summary>
---
```

Optional keys (allowed, no requirement to include):
`disable-model-invocation`, `generated_at`, `metadata`, `license`,
`argument-hint`, `allowed-tools`, `effort`, `user-invocable`, `context`,
`agent`, `origin`.

The `description` field may be a single line OR a YAML folded scalar (`>`).
Pointer adapters copy the canonical `description` verbatim (plus a one-line
adapter notice) so discovery semantics match across runners. Use runner-neutral
phrasing such as "the agent runner" or "the current session" in canonical
descriptions rather than naming a specific runner.

This minimal canonical shape was chosen because (a) it matches the only
prior frontmatter guidance in the repo
(`.opencode/skills/codebase-review-swarm/README.md:32` — "required `name`
and `description`, plus harmless metadata"), (b) it minimizes churn across
the existing SKILL.md files, and (c) the skill loaders in all three
runners accept both shapes.

## Thin-pointer adapter pattern

When a runner's own discovery tree needs a skill whose canonical home is a
different tree, that runner's tree holds a thin pointer, not a copy:

1. The canonical protocol lives in ONE tree (see the Canonical homes table).
2. The other tree holds a short `SKILL.md` whose frontmatter copies the
   canonical `description` (so discovery matches) and whose body names the
   canonical path — e.g. `.agents/skills/<skill-name>/SKILL.md`.
3. Keep the pointer under 30 lines; it must contain the literal canonical
   path string.

`backend/tests/test_skill_tree_collapse.py` enforces that every repo-specific
skill's `.claude` pointer exists, stays thin, names an existing canonical,
and that no `.opencode` mirror reappears. `codebase-review-swarm` is the
longest-standing exemplar (canonical `.opencode`, thin adapters in
`.agents`/`.claude`).

## Discovery coverage

Which tree each runner's documented discovery path loads:

| Runner | Discovery path(s) | What it finds |
|---|---|---|
| Codex | `.agents/skills/` + `AGENTS.md` | repo-specific canonicals, `.agents` runner-specific skills, adapter pointer |
| ZCode | `.agents/skills/` | same as Codex |
| opencode-swarm | plugin directory discovery of `.opencode/skills/<name>/SKILL.md`, plus native discovery of `.claude/skills/` and `.agents/skills/` | plugin runner-specific + generated + adapter canonical from `.opencode`; repo-specific canonicals via `.agents` |
| Claude Code | `.claude/skills/` + `CLAUDE.md` (which imports `AGENTS.md`) | runner-specific + framework-vendored canonicals + thin pointers |

Caveat: the opencode native multi-tree discovery claim is grounded in the
2026-09 audit's runner-docs research (R5-S10 in issue #569) and
`.opencode/skills/codebase-review-swarm/README.md:31` (portable-install
paths), not in a locally executable loader probe. The automated guard is
therefore structural — canonical homes plus valid pointers — which is the
strongest feasible rung without launching each runner. If a runner gains or
changes discovery paths, update this table and
`backend/tests/test_skill_tree_collapse.py` together.

## `.secretscanignore` validation contract

`scripts/check_secretscan.py` enforces these properties on every PR:

| ID | Property | Severity |
|---|---|---|
| C-SSECRETSCAN-1 | `.secretscanignore` exists and every non-comment non-blank line is a syntactically permissible glob (non-empty) | fatal |
| C-SSECRETSCAN-2 | Adversarial positive samples (`backend/tests/conftest.py`, `.env.example`, `.opencode/skills/codebase-review-swarm/README.md`) each match at least one pattern | fatal |
| C-SSECRETSCAN-3 | Adversarial negative samples (chosen to exercise `**` segment boundaries) match NO pattern | fatal |
| C-SSECRETSCAN-4 | Patterns matching > 50% of tracked files trigger an overly-broad-glob warning | advisory |
| C-SSECRETSCAN-5 | Literal-path patterns matching no file, OR `<dir>/**` patterns where `<dir>` does not exist, trigger a stale-glob warning | advisory |

To silence a C-SSECRETSCAN-5 warning for a pattern that legitimately matches
nothing today but guards a path that may appear in another checkout or
future state, add a trailing comment to the pattern:

```
config.example.json  # defensive: optional example, may not exist in every checkout
```

The `defensive:` marker is the supported way to mark a pattern as
intentional. Patterns containing wildcards are inherently defensive and are
not flagged unless they target a nonexistent top-level directory.

The validator uses a hand-rolled gitignore-style glob matcher (stdlib only),
not `git check-ignore` (which reads `.gitignore` additively and cannot
isolate `.secretscanignore`'s semantics) and not `pathspec` (not currently a
dependency; adding it would touch `backend/requirements-lock.txt`).

## AC traceability format

Skill files that document acceptance criteria SHOULD include a
`tracked_by:` reference pointing to the automated test that proves the
criterion is met:

```
## Acceptance criteria

- The auth bridge raises the correct exception on stale tokens.
  tracked_by: backend/tests/test_auth_routes.py:442
```

When no automated test exists for a criterion, the `tracked_by:` line may
be omitted. Reviewers can then search for the criterion manually; the
absence of `tracked_by:` is itself a signal that the criterion is
documentation-only.

Apply this convention to all skill files with AC sections; do not skip a
file because the AC is "obvious."

## Skill size budgets

The agent-skills specification recommends keeping a `SKILL.md` lean (order
of ~5k words) and moving reference detail into bundled files
(`references/`, `assets/`). Measured at the #569 collapse (lines per
canonical SKILL.md): all repo-specific and framework-vendored skills are
within budget except `swarm-pr-review` (888 lines; the corresponding
user-level copies are the ones agents actually load in most sessions) and
`issue-tracer` (393 lines, close to budget). Both are flagged as known debt:
rewriting live agent instructions is a content change with regression risk
and no failing check driving it, so they are recorded here rather than
silently trimmed. When editing these skills, prefer splitting detail into
`references/` files over growing the SKILL.md body.

## What is intentionally NOT here

- **`.opencode/skill-routing.yaml`**: documented absent in `AGENTS.md`. The
  opencode-swarm plugin uses directory-based skill discovery
  (`.opencode/skills/<name>/SKILL.md`); there is no consumer for a routing
  YAML and adding one would be unwired. Re-evaluate only if the
  opencode-swarm plugin gains an `audience:`-aware loader (per
  `docs/releases/pending/skills-narrowed-directives.md`).
- **A separate sync/CI drift gate**: the byte-mirror invariant the old
  local-sync-plus-CI-wrapper pair enforced no longer exists by
  construction (one canonical home per skill). The enforcement surface is
  `backend/tests/test_skill_tree_collapse.py` in the Backend CI job;
  adding a second gate over the same invariant would be redundant.
