# Skill Conventions

Canonical specification for repo-specific skills across the three agent-runner
trees (`.claude/skills/`, `.agents/skills/`, `.opencode/skills/`). Referenced
by `AGENTS.md`, `docs/engineering/conventions.md`, and enforced in CI by
`scripts/check_skill_sync.py`. This document is the source of truth for:

- What counts as a repo-specific skill vs a runner-specific or framework-vendored skill
- The mirror rule and its exceptions
- Canonical-tree precedence for sync propagation
- Frontmatter shape
- The adapter-skill pattern
- `.secretscanignore` validation contract
- AC traceability format

## Scope categories

Every skill directory under any of the three trees falls into exactly one
category. The category determines whether the mirror rule applies.

| Category | Mirror rule applies? | Examples |
|---|---|---|
| **Repo-specific** | YES — must be byte-identical across all three trees when present in two or more | `swarm`, `qa-sweep`, `engineering-conventions`, `commit-pr` |
| **Runner-specific** | NO — legitimately lives in one tree only | `.claude/`: `coding-agent`, `ship`, `github`, `reviewing-*`; `.agents/`: `contributing`, `subprocess-safety`; `.opencode/`: `deep-research`, `loop`, `swarm-pr-subscribe` |
| **Framework-vendored** | NO — vendored from the upstream swarm framework; reference deleted `.swarm/` and absent `src/agents/architect.ts`; NOT in `AGENTS.md`'s repo-specific list | `brainstorm`, `clarify`, `plan`, `execute`, `council`, `critic-gate`, `phase-wrap`, `pre-phase-briefing`, `resume`, `specify`, `consult`, `deep-dive`, `design-docs`, `discover`, `issue-ingest`, `clarify-spec` |
| **Adapter** | Adapter rule applies (see below) — one tree holds the canonical protocol; other trees hold thin pointers | `codebase-review-swarm` |
| **Generated** | NO — auto-generated knowledge skills under `.opencode/skills/generated/`; the sync tool skips this subgroup entirely | the 12 `generated/*` skills |

The current category lists are encoded as Python constants in
`scripts/sync_skills.py` (`RUNNER_SPECIFIC_ALLOWLIST`,
`FRAMEWORK_VENDORED_ALLOWLIST`, `ADAPTER_SKILLS`, `GENERATED_SUBGROUP`).
Adding a skill to a non-mirror category requires editing that script.

## Mirror rule (intent-based)

> A repo-specific skill that is present in two or more trees must be present
> in all three trees with byte-identical content.

This is *intent-based*, not literal-counts-equal. The three trees legitimately
have different sizes because each runner has its own runner-specific and
framework-vendored skills. The rule catches real drift (a repo-specific skill
edited in one tree but not the others, or present in two trees but missing
from the third) without forcing every tree to carry every other runner's
single-tree skills.

Per-tree **frontmatter `description:`** wording differences are forbidden for
non-adapter repo-specific skills. Use runner-neutral phrasing such as "the
agent runner" or "the current session" rather than naming a specific runner
("Claude Code", "Codex", "opencode-swarm"). This keeps the skill portable and
avoids drift.

**Body-level references are scoped differently.** A skill body may legitimately
name the primary runner's actual runtime paths (for example
`.zcode/session/swarm-mode.md` for this repo's primary runner, ZCode) when
that path is the real file the skill writes. Two rules govern body paths:

1. The path must be **the actual runtime file** for this repo's primary runner
   (verifiable on disk — e.g. `.zcode/session/swarm-mode.md` exists).
2. The path must be **identical across all three trees** (so the mirror rule's
   byte-identical requirement holds). Per-runner portability is a secondary
   concern; if a secondary runner (Claude Code, Codex, opencode-swarm) needs
   a different session path, that's a follow-up adaptation the secondary
   runner's plugin can make, not a violation of this spec.

When a body path is primary-runner-specific, prefer adding a one-line comment
naming the runner ("for this repo's primary runner, ZCode") so a future
contributor doesn't read it as universal.

## Canonical-tree precedence

When `scripts/sync_skills.py` propagates a drifted skill, it picks the
canonical source from the first tree (in precedence order) that contains the
skill:

```
.agents/skills > .claude/skills > .opencode/skills
```

Rationale: `docs/releases/pending/skills-narrowed-directives.md` documents
that `.opencode/skills/` is the opencode-swarm plugin's internal area and is
skipped when injecting skills for Claude Code or Codex sessions. Using
`.opencode` as the canonical source would propagate from a tree that the
target runners ignore. `.agents/skills/` is the smallest and most
repo-focused tree; `.claude/skills/` is consumed by Claude Code.

### Per-skill canonical override

When the maintainer wants to preserve content found only in a non-precedence
tree (for example, a useful addendum present in `.opencode` but absent from
`.agents`), add an entry to `PER_SKILL_CANONICAL` in `scripts/sync_skills.py`:

```python
PER_SKILL_CANONICAL = {
    "ci-fix-monitor": ".opencode/skills",   # preserves the force-push addendum
    "commit-pr": ".claude/skills",          # runner-neutral branch-prefix wording
}
```

Each override MUST carry a comment documenting why. The override is the
supported escape hatch; do not introduce a second copy of the rule logic.

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
Per-skill per-tree description wording differences are forbidden for
non-adapter repo-specific skills (see "Mirror rule" above).

This minimal canonical shape was chosen because (a) it matches the only
prior frontmatter guidance in the repo
(`.opencode/skills/codebase-review-swarm/README.md:32` — "required `name`
and `description`, plus harmless metadata"), (b) it minimizes churn across
the existing 120+ SKILL.md files, and (c) the skill loaders in all three
runners accept both shapes.

## Adapter-skill pattern

Some skills are too large or too runner-coupled to mirror byte-identically.
For those, declare the skill in `ADAPTER_SKILLS` in `scripts/sync_skills.py`
and follow this pattern:

1. The canonical protocol lives in ONE tree (for `codebase-review-swarm`,
   that is `.opencode/skills/codebase-review-swarm/` per `AGENTS.md:30` and
   `.opencode/skills/codebase-review-swarm/INSTALL.md`).
2. The other two trees hold thin adapters: short SKILL.md files that point
   to the canonical path.
3. `scripts/check_skill_sync.py` enforces the adapter relationship:
   - The non-canonical copy's body MUST contain the literal string
     `.opencode/skills/<skill-name>/` (a pointer to canonical).
   - The non-canonical copy MUST be less than 30% of the canonical copy's
     line count (thinness check).

If a future skill needs the adapter pattern, add it to `ADAPTER_SKILLS` and
verify both adapter invariants hold.

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

## Sync tooling

`scripts/sync_skills.py` has three modes:

- **default** (no flag): propagates the canonical copy of each drifted
  repo-specific skill to every tree missing or divergent. Idempotent —
  running twice is a no-op.
- `--check`: reports drift to stderr without writing. Exit 1 on drift,
  0 when clean. This is the CI mode invoked by
  `scripts/check_skill_sync.py` from the Quality contracts job.
- `--dry-run`: prints planned changes without writing.

To run locally:

```bash
python scripts/sync_skills.py --check     # CI gate; report drift
python scripts/sync_skills.py --dry-run   # preview propagation
python scripts/sync_skills.py             # propagate
```

## What is intentionally NOT here

- **`.opencode/skill-routing.yaml`**: documented absent in `AGENTS.md`. The
  opencode-swarm plugin uses directory-based skill discovery
  (`.opencode/skills/<name>/SKILL.md`); there is no consumer for a routing
  YAML and adding one would be unwired. Re-evaluate only if the
  opencode-swarm plugin gains an `audience:`-aware loader (per
  `docs/releases/pending/skills-narrowed-directives.md`).
- **pytest tests for the check scripts**: the existing `scripts/check_*.py`
  family has no pytest coverage; the contract is the CI exit code. Follow
  that convention unless a separate decision establishes a test baseline.
