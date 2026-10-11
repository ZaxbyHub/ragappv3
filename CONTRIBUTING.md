# Contributing to KnowledgeVault (RAGAPPv3)

Thanks for your interest in contributing! KnowledgeVault is a self-hosted RAG
service for ingesting technical documents and chatting with them using local
LLMs. This guide covers how to set up, make changes, and open a good PR.

> **AI coding agents** (Codex, Claude Code, opencode-swarm) should start with
> [`AGENTS.md`](AGENTS.md) and the `engineering-conventions` / `commit-pr`
> skills. This file is the human-facing version of the same process.

<!-- CANONICAL-VERSION: ZaxbyHub/opencode-swarm@e5278b2522 (PR #3185). Verbatim-copy core; repo notes are repo-specific. Update by re-copying from the canonical and bumping this stamp. -->

## Filing issues — issues are executable contracts

Issues in this repository are not prose requests: they are **executable contracts**. A well-formed issue can be frozen into acceptance checks, proven RED at the base commit, and proven GREEN at the head of its PR, with independent reviewer and critic gates in between. Every criterion that can be executable must be bound to a check — a criterion with a fake or missing binding is miswritten. Criteria that genuinely cannot be executable take the NON-EXECUTABLE path below instead. This section defines the contract; the tracer workflow consumes this shape. Existing issues filed before this contract are grandfathered; it governs new issues.

**Where this sits in the field.** The acceptance-criteria grammar below is [EARS](https://alistairmavin.com/ears/) (Easy Approach to Requirements Syntax — Mavin/Rolls-Royce, 2009; used across aerospace and heavyweight systems engineering). The overall shape — specification before plan, plan before tasks, human gates around implementation — is the same family as [GitHub Spec Kit](https://github.com/github/spec-kit)'s constitution → specify → plan → tasks → implement → converge. Our differentiator: the specification lives in the tracker (the issue itself), and acceptance is **proven executably** (frozen checks run RED at base, GREEN at head) rather than by implementation review alone. The one-AC-one-check rule is classical requirements traceability: every requirement gets exactly one chosen verification method.

### Two issue types

1. **Standalone issues** (feature, bug, investigation): the required core below, no title convention beyond a clear outcome-oriented statement.
2. **Workstream program issues**: when a program spans multiple PRs, it is decomposed as one issue per PR with the title ``[Workstream <letter>] PR <n> of <m>: <outcome statement>`` — workstreams are parallel-capable tracks, PRs within a workstream are sequential, and **one issue = one PR**. Each body carries a `## Sequence` section naming the full chain (`#A -> #B -> ...`) and its depends-on/blocks relationships. The umbrella tracker/epic issue (workstream table, closure ledger, maintainer-gated items, checklist) is repo-specific; see the repo notes at the end of this file.

### Body anatomy (required core)

Every verifiable issue has, in this order:

1. **Provenance preamble** (italic, first line): where the issue was generated from (audit run, session, plan), the exact commit every claim was re-checked against (full SHA), the one-issue-one-PR statement, and a disclaimer for any referenced local artifacts that are not in this repository.
2. `## Summary` — what will be true after this PR that is not true today. Outcome-framed, not task-framed.
3. `## Problem (claims to verify)` or `## Current state (evidence)` — numbered claims, each carrying:
   - a file:line reference, as a permalink anchored to the full SHA named in the preamble;
   - a code excerpt for load-bearing lines (the lines a fix will touch or a check will read);
   - an **epistemic label** — `VERIFIED` (re-checked at the anchor commit; say how) or `INFERENCE` (motivating but unproven) — so the implementer knows which premises still need proving;
   - a **prior-art reconciliation** note when a claim overlaps existing implementations (name them and state which one this PR extends rather than duplicates);
   - corrections discovered during review are **preserved inline** (`CORRECTED during review: ...`), never silently rewritten — the issue is a living record of what was claimed and what was verified.
4. `## Scope` — `In:` and `Out:` (out-of-scope items reference their owning issue when they have one).
5. `## Acceptance criteria` — the contract (next subsection). Non-negotiable.
6. `## Sequence` — programs only (above).

### The acceptance-criteria contract

Each criterion is a checkbox line in **EARS grammar**:

```
- [ ] ACn: WHEN <trigger>, THE SYSTEM SHALL <observable response>
```

- Sequential from `AC1`, nothing between the id and the colon, each AC appears exactly once. One AC binds to exactly **one** frozen check — never share a check across ACs.
- EARS patterns, in order of preference for this repo: **event-driven** (`WHEN <trigger>, THE SYSTEM SHALL <response>` — the default), **ubiquitous** (`THE SYSTEM SHALL <response>` — always-true properties; the natural shape for PRESERVING criteria), **unwanted-behavior** (`IF <undesired trigger>, THEN THE SYSTEM SHALL <response>` — error paths), **state-driven** (`WHILE <state>, THE SYSTEM SHALL <response>`), **optional-feature** (`WHERE <feature>, THE SYSTEM SHALL <response>`).
- The response must be **observable** — a test can assert it. "The system shall be more robust" is not a response.

Each AC carries exactly five sub-bullets:

```
- [ ] AC1: WHEN <trigger>, THE SYSTEM SHALL <observable response>.
  - class: DISCRIMINATING | NEW-SURFACE | PRESERVING
  - argv: <one runnable command from the repository root — the frozen check driver>
  - expect: <the signature the BASE leg shows before the fix>
  - files: <files the check reads or creates>
  - why: <why this is red at base — or green at both ends for PRESERVING>
```

| class | meaning | base leg | head leg |
|---|---|---|---|
| `DISCRIMINATING` | the defect exists today | **RED** (the failure signature) | GREEN |
| `NEW-SURFACE` | the fix creates a surface that cannot exist at base (new spec, new tool) | deterministic **absence** signature — the runner's literal no-matches output (e.g. vitest's `No test files found`, bun's `The following filters did not match any test files:`) | GREEN |
| `PRESERVING` | a regression floor | GREEN | GREEN |

Honest-class rules:

- Do not label an AC `DISCRIMINATING` unless it is genuinely red at the anchor commit — run the check mentally (or for real) against base before claiming it.
- A `NEW-SURFACE` AC must fail at base with a **deterministic** absence signature, never an environmental error (missing runtime, network). If the driver cannot exist at base, the driver itself prints a sentinel line and exits nonzero.
- `PRESERVING` criteria are regression floors: they must be green at base and stay green at head. A PRESERVING check that regresses at head is a fix bug, not a check bug.
- **No vacuous checks.** An AC shaped "do X and observe failure" that cannot fail on the primary dev/test runtime is a false contract — route the probe through a context where it can genuinely fail, or reshape the AC.

**NON-EXECUTABLE criteria.** Some criteria have no meaningful executable check — docs-only changes, host-only behavior (GUI/TUI surfaces a check cannot drive), maintainer product decisions, unavailable external services. For those, write `class: NON-EXECUTABLE(<reason>)` with the closed reason enum `DOCS_ONLY | HOST_ONLY | PRODUCT_DECISION | EXTERNAL_SERVICE_UNAVAILABLE`, replace `argv`/`expect` with **named substitute evidence** (a review artifact, a decision recorded on the issue, a captured external state), and get plan-critic approval for the substitution — never force an artificial executable check; that is the vacuous shape the rule above forbids.
- `argv` is one command, runnable from the repository root, POSIX-safe, deterministic (use the repo's test-runner flags for hermetic runs — see repo notes). `expect` describes what the **base** leg shows; the head leg's pass condition lives in the check's own exit logic.

### Evidence rules

- Every claim is anchored: permalinks embed the full SHA; before filing, re-verify each claim at that commit and say so in the preamble.
- Quotes and excerpts must exist verbatim in the cited file at the cited lines — grep each one before filing (a fabricated quote propagates into every downstream ledger).
- When line numbers matter, confirm the cited file is unchanged between the anchor commit and filing (or re-anchor).
- Label every non-trivial claim `VERIFIED` or `INFERENCE`.

### Extended-section menu (recommended for design-heavy and program issues)

The core is the required minimum. For issues whose fix has real design surface, add any of these (they **inform**, the ACs **bind** — if a Design section and an AC disagree, the AC wins and the Design section gets corrected):

`## Why (evidence)` · `## Current behaviour` · `## Dependencies` (`Depends on:` / `Gated on:` / `Blocks:`) · `## Design` (numbered, with exact signatures) · `## Files to change` · `## Tests` (named spec files) · `## Fixtures to add` · `## Local verification recipe` (commands) · `## Release rules` · `## Definition of done`

### Filing checklist (self-review before Create)

- [ ] Preamble names the source, the anchor SHA, and re-verification
- [ ] Every claim has a full-SHA permalink; every quote greps clean at that SHA; VERIFIED/INFERENCE labeled
- [ ] Scope has both In and Out
- [ ] ACs sequential, each exactly five sub-bullets, one-AC-one-check
- [ ] Classes honest (DISCRIMINATING genuinely red at base; NEW-SURFACE deterministic absence; PRESERVING green-both-ends)
- [ ] No vacuous checks; argv runnable from repo root; expect describes the base leg
- [ ] Program issues: title `[Workstream <letter>] PR n of m`, Sequence section, one issue = one PR
- [ ] Any NON-EXECUTABLE criterion carries a closed-enum reason and named substitute evidence (not a forced check)

### Repo notes (ragappv3)

- **argv conventions for this repo**: backend checks run as `cd backend && python -m pytest <file>::<test> -q -p no:randomly -p no:cacheprovider --basetemp=<scratch-dir>`; frontend checks as `cd frontend && npx vitest run <file>`.
- **Tracker convention**: program tracker issues are titled `[Epic] ...` (e.g. the frontier-audit remediation epic); workstream letters are allocated per program.
- **Provenance convention**: remediation issues carry an `## Audit provenance` section naming the finding IDs, their severities, and the snapshot + master-recheck SHAs — keep that discipline; the epistemic VERIFIED/INFERENCE labels extend it to the claim level.
- A `NEW-SURFACE` argv naming a spec absent at base fails with pytest's `no tests ran`/file-not-found class of output — write that signature verbatim in `expect`.

---

## Project layout

- `backend/` — Python 3.11, FastAPI + SQLite + LanceDB
- `frontend/` — React + TypeScript + Vite (Vitest, shadcn/ui + Tailwind)
- `scripts/` — repo contract checks and maintenance scripts
- `docs/` — user and engineering docs

Engineering conventions and the testing policy are documented in
[`docs/engineering/conventions.md`](docs/engineering/conventions.md) and
[`docs/engineering/testing.md`](docs/engineering/testing.md). Please read them
before non-trivial changes — match the existing pattern in the file you're
editing.

## Prerequisites

- **Python 3.11** (CI pins 3.11 — see the caveat below)
- **Node.js ≥ 22.22.0** (`frontend/package.json` `engines`; the runtime pin
  is enforced across CI, both Dockerfiles and this doc by
  `scripts/check_runtime_contract.py`)
- Local LLM/embedding services (Ollama + embedding container) or Docker — see
  [`README.md`](README.md) and [`INSTALLATION.md`](INSTALLATION.md)

## Local setup

Full setup (including LLM/embedding services and Docker) is documented in
[`INSTALLATION.md`](INSTALLATION.md) and [`README.md`](README.md). The essentials:

**Backend** (from `backend/`):

```bash
python -m venv venv
# Windows: venv\Scripts\activate   |   macOS/Linux: source venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt   # ruff, pytest-cov, pytest-asyncio, requests
python -m uvicorn app.main:app --host 0.0.0.0 --port 9090
```

**Frontend** (from `frontend/`):

```bash
npm ci          # or: npm install
npm run dev
```

**Environment:** copy `.env.example` to `.env` and fill in the required
secrets. At minimum set `JWT_SECRET_KEY` and `ADMIN_SECRET_TOKEN` — generate
each with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

On Windows, `start-services.ps1` / `stop-services.ps1` orchestrate the backend,
frontend, and embedding/LLM services together.

## Branch, commit, and PR conventions

- **Base branch:** `master`. Branch from the latest `origin/master`.
- **Branch names:** short, prefixed slugs (e.g. `fix/tag-cascade`, `feat/bulk-export`).
- **Commits:** [Conventional Commits](https://www.conventionalcommits.org/) —
  `type(scope): lowercase description`, where `type` is one of
  `feat`, `fix`, `test`, `docs`, `refactor`, `chore`.
- **Scope your commits:** stage explicit paths; don't commit IDE files, local
  settings, `.env`, or generated artifacts.
- **PRs target `master`** and should include:
  - `## Summary` — 1–3 bullets on what changed and why.
  - `## Test plan` — a checklist of the commands you ran and their results.
- Never `git push --force` to a shared branch; use `--force-with-lease` if you
  must rewrite your own PR branch.

## Before you push — run the CI gates locally

CI (`.github/workflows/ci.yml`) runs eight jobs — Frontend, Playwright e2e
smoke, Playwright a11y matrix, Quality contracts, Detect docker scope, Docker
build smoke, SAST (bandit), and Backend (full inventory:
`docs/engineering/testing.md`, section 4). Reproduce them locally so your PR
goes green on the first try:

**Backend** (from `backend/`):

```bash
ruff check .
# CI runs the FULL suite (plus the tests for the area you changed locally):
pytest --tb=short -n auto tests/
```

**Frontend** (from `frontend/`):

```bash
npm run typecheck
npm run lint        # zero-warning gate: eslint src --max-warnings 0
npm test            # vitest run
npm run build
```

**Quality contracts** (from repo root):

```bash
python scripts/check_runtime_contract.py   # runtime pins: Docker/CI/engines/docs parity
python scripts/check_config_contract.py
python scripts/check_settings_consumers.py   # every Settings field has a production read site
python scripts/check_batch_mock_seq.py   # send-path batch mocks mirror the save response contract
python scripts/check_pr_scope_drift.py
python scripts/check_sast_baseline.py   # SAST baseline/scope/workflow integrity
python scripts/check_secretscan.py      # .secretscanignore validity
python scripts/check_test_collection_scope.py   # all pytest files live under backend/tests/
python scripts/check_a04_http500_detail_hygiene.py   # no raw-exception 500 details
python scripts/check_b03_upload_migration_timeout.py   # upload migration timeout contract
python scripts/check_l05_raw_palette.py   # raw Tailwind palette classes within budget
python scripts/check_l05_page_headers.py   # pages use the shared PageTitleHeader
python scripts/check_l07_a11y_gate_wired.py  # the a11y matrix gate is wired in CI
```

**Closure evidence gate** (from repo root; standalone workflow
`.github/workflows/closure-evidence.yml`, issue #568 — evaluates PRs whose
bodies close issues; warn-mode rollout):

```bash
python scripts/check_closure_evidence.py evaluate --pr-data <snapshot.json> --mode warn
python scripts/check_closure_evidence.py verify-test --repo . --base <pre-fix-sha> --head <head-sha> --test backend/tests/test_x.py::test_y
```

**SAST** (from repo root; requires `bandit` from `backend/requirements-dev.txt`):

```bash
python scripts/run_bandit.py
# Fails on NEW findings vs backend/security/bandit-baseline.json, and on
# UNUSED `# nosec` suppressions: a marker that suppresses nothing. Bandit's
# "nosec encountered ..., but no failed test" warnings name the candidate
# sites; each is cross-checked with an --ignore-nosec re-scan (the warning
# stream alone has per-context false positives), and only truly-unused
# markers fail the gate. Delete the dead marker (keep the safety rationale
# as a plain comment) or fix the code it claims to suppress.
# If you intentionally accept a new pre-existing finding, regenerate with
#   python scripts/run_bandit.py --update-baseline
# and justify the newly-suppressed finding IDs in your PR. The regen also
# prints an advisory listing nosec-usage warning sites worth reviewing.
```

## Testing expectations

New behavior ships with tests, and tests assert real behavior (status **and**
body **and** state for the backend; callback args / DOM for the frontend), not
just that something rendered. Cover the negative paths. See
[`docs/engineering/testing.md`](docs/engineering/testing.md) for the test
harness patterns and exemplars.

> **Python version caveat:** CI pins **Python 3.11**. On a newer local
> interpreter (e.g. 3.14) some tests fail with
> `RuntimeError: There is no current event loop` — this is a local-interpreter
> artifact, **not a regression**. Prefer a 3.11 virtualenv. Details in
> `docs/engineering/testing.md`.

## Windows development

Windows (Git Bash, CRLF checkouts) is a supported development environment
with documented seams — the CI contract runs on Linux, and the seams below
are the known places where a Windows shell behaves differently (issue #567):

- **Git Bash rewrites leading-slash environment values.**
  `VITE_APP_BASENAME=/knowledgevault npm run build` fails because MSYS
  converts the value to a Windows path (`C:/Program Files/Git/knowledgevault`)
  before Node sees it — the base-path validators reject that value, and since
  #567 the thrown error names the received value so the rewrite is visible.
  Prefix the command with `MSYS_NO_PATHCONV=1` (see
  `docs/engineering/testing.md` and the `ci-compatibility-audit` skill), or
  run it from PowerShell.
- **Backend lockfiles are universal.** `pip install -r
  backend/requirements-lock*.txt` works on Windows and Linux alike
  (`docs/engineering/lockfiles.md`); the `justfile` at the repo root mirrors
  the CI steps locally, and `.devcontainer/devcontainer.json` provides a
  known-good Linux environment if you would rather skip the seams entirely
  (its feature-digest pins live in `.devcontainer/devcontainer-lock.json`,
  regenerated automatically by the devcontainer CLI whenever the container is
  built after a feature-version change).
- **A few tests are platform-sensitive** (symlink-privilege and
  backslash-traversal cases); see `docs/engineering/testing.md`.
- **A green backend run can still exit 1.** On Windows, pytest's tmp-dir
  cleanup can raise `PermissionError [WinError 5]` on
  `pytest-of-<user>\pytest-current` during teardown — after every test
  passed, with no FAILED lines and no summary. Re-run with `--basetemp`
  pointed inside the repo (e.g. `--basetemp=.swarm/pytest-tmp`); see
  `docs/engineering/testing.md`.

## Reporting issues

Open a GitHub issue with: what you expected, what happened, steps to reproduce,
and your environment (OS, Python/Node versions). For security-sensitive
reports, please avoid filing public details until maintainers can respond.

## License

This repository does **not** currently include a `LICENSE` file. Until one is
added, contribution terms are at the maintainers' discretion — please open an
issue if you need clarity before contributing substantial work.
