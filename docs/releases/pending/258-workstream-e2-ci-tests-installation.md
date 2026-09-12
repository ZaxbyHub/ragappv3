# fix(ci): workstream E2 — CI integration, test-theater repairs, installation docs (#258)

Branch `zcode/258-workstream-e-ci-tests-installation` (base a543361). This
note covers all seven plan groups (G1–G7) of the approved fix plan
(`.agents/issue-traces/258-ci-integration-tests-installation/07-approved-plan.md`).

## What changed

### G1 — test-theater repairs, bound to production paths (TEST-001/002/007)

- **`filter_relevant` now enforces an explicitly passed `top_k`** (slices the
  returned sources at return) — a production change closing TEST-001: the
  helper accepted `top_k` but never capped. The `None` default is the
  **documented contract** (docstring): callers that omit `top_k` — the
  agentic RetrievalTool by design, the main query path via its token-budget
  truncation — receive the full within-threshold set. New additive pin file
  asserts both halves (explicit top_k slices exactly; omitted top_k returns
  uncapped, intentional). The NaN-disjunction assertion became an exact
  `== 0.3` pin (C1b).
- TEST-002's sleep/timestamp body replaced with a DB-derived net-count
  assertion plus a patched-clock TTL expiry (no wall-clock reliance).
- TEST-007's tautological node (`assert len(embeddings) >= 0` behind a mock
  of the unit under test) rewritten at the real HTTP boundary, asserting
  per-text uniqueness/order through a real split (C4 template).

### G2 — production-binding (TEST-003/005/008)

- `validate_fts_index(table) -> bool` extracted from the inline lifespan
  block (behavior-preserving logging; the new bool return is not inspected
  by any production caller — pinned by a test asserting the lifespan call
  site ignores it). `test_lifespan_fts_validation.py` now tests the real
  function; its 6 inline copies are deleted.
- `test_wiki_events.py`: the 6 inline event-generator replicas replaced with
  production-route-driven SSE tests (bus unit tests kept).
- `test_email_service.py`: `TestSaveAttachmentSentinel` moved to
  `IsolatedAsyncioTestCase` (its 4 async bodies never executed); the true
  async-on-sync-base census was re-derived with `rg` before conversion —
  the originally named siblings were verified NOT affected.
- `test_integration.py`: mocked upload-index-chat and mocked-deletion tests
  replaced with real-worker/real-store versions.

### G3 — build/CI contract (BUILD-001/002, TOOL-001)

- Runtime re-pin: **Node.js 22.11.0 LTS (minor-exact) + Python 3.11** across
  ci.yml `setup-node`/`setup-python`, root `Dockerfile`,
  `frontend/Dockerfile`, `frontend/package.json` engines and CONTRIBUTING.md.
  `scripts/check_runtime_contract.py` is the single mechanical decision
  point (ALLOWED_RUNTIME pin table), wired into the Quality-contracts CI
  job. Docker images are digest-pinned; Node 20 (EOL 2026-04-30) and the
  python:3.14 drift are gone.
- New docker build smoke job (paths: `Dockerfile/**`, `docker-compose.yml`,
  `frontend/Dockerfile`); ci.yml gained `push: master` and `merge_group`
  triggers.
- BUILD-001: `tsconfig.node.json` includes `vite.paths.ts`;
  `ApiProxyOptions.rewrite` is optional.
- TOOL-001: `sync_skills.py` requires ALL adapter trees for
  ADAPTER_SKILLS entries (canonical-with-zero-adapters no longer passes).
- Dependabot: base-image majors ignored with a 14-day cooldown on the docker
  ecosystems (major moves are gated by the runtime-contract script).

### G4 — frontend product (TEST-006, ENH-005/011, FU-009, legacy-14)

- Type contracts suite: `tsconfig.contracts.json` +
  `interfaces.contracts.ts` with `@ts-expect-error` negative-compile pins;
  the always-true `api.interfaces.test.ts` is removed; `typecheck:contracts`
  runs in CI (C7).
- a11y: DocumentsTableSkeleton row checkboxes named, UploadDropzone file
  input labeled, context aria-labels on the composite skeletons, PageLoader
  `role=status` (C21b/C22).
- **FU-009 root-fix**: the additions virtualization test rewritten to the
  rerender pattern (matching the non-flaky removals test) — 50 sequential
  mounts driving real polling timers are gone; stability evidence is
  repeated green runs.
- legacy-14: DocumentsPage list-fetch ErrorState retry; command palette
  (Ctrl/Cmd+K, navigation commands, wired at the app shell); per-route
  Suspense fallbacks use page-level skeletons where they exist. The four
  preserved behaviors (abort-on-switch, rAF streaming, pagination, delete
  flushing) untouched and covered by their existing suites (C25).

### G5 — frontend coverage gate (ENH-006)

`@vitest/coverage-v8` added; coverage scoped to `src/lib/api/**` +
`src/stores/**` with thresholds at measured baseline minus a small margin;
an additive check pins the substantive design (scope globs + threshold
floor) so the gate cannot be zeroed; CI step wired (C11).

### G6 — nightly tier (ENH-007, legacy-10)

`.github/workflows/nightly.yml` (schedule + manual dispatch): full-dependency
job (installs `requirements.txt` including `unstructured[all-docs]`) running
the committed real-docs fixture parser tests, the FULL backend suite, and a
cross-file isolation run. New binary fixtures include `scanned_page.pdf`
(image-only, zero text) — the tier asserts zero text chunks surface for it
(parser-behavior evidence, not silent pass). `scripts/parser_bakeoff.py`
committed (unstructured fast vs hi_res on the same fixtures by default; Docling and marker are optional backends the harness probes and skips with an explicit note until their dependencies are added to the nightly install,
emitting a committed benchmark markdown) (C12/C13).

### G7 — docs (DOC-001..003, ENH-001, ENH-003)

- **INSTALLATION.md rebuilt on the maintained flow** (validated by the new
  `scripts/check_installation_doc.py`, wired into the Quality-contracts CI
  job): versions aligned to the runtime contract (Node 22.11 / Python 3.11
  across the prerequisites table, brew/nodesource setup, LTS guidance); the
  zero-arg `init_db()` snippets (which raised TypeError —
  `init_db(sqlite_path)` is required) replaced with
  `python -c "from app.models.database import init_db; init_db('./ragapp.db')"`
  plus a note that `uvicorn app.main:app` auto-initializes via the
  application lifespan; the self-contained compose recipe — which referenced
  an undefined `harrier-embed` service, configured models it never pulled,
  and built non-existent Dockerfiles — replaced by `docker compose up -d`
  against the repo's maintained `docker-compose.yml` (harrier-embed /
  reranker / redis / knowledgevault) with a short excerpt of the env wiring
  and host-side model-pull instructions that match the configured
  CHAT_MODEL/INSTANT_CHAT_MODEL.
- **`docs/engineering/lockfiles.md`** (new, ENH-001): the lockfile
  regeneration procedure (pip-compile commands mirroring ci.yml's dry-run
  verification), the platform rule — regenerate ONLY on Linux/CI; Windows
  regeneration produces platform-specific lockfiles (the nvidia/cuda family
  via `unstructured → unstructured-inference → cuda-toolkit` resolves
  Linux-only wheels; commit `da0afc4` is the in-repo precedent) — and the
  update/rollback procedures. Lockfiles themselves unchanged (already 100%
  hash-pinned; AC19 is a preserving pin).
- **ENH-003: `backend/embedding_server/` removed** (Dockerfile,
  requirements.txt, server.py — dormant; `backend/app` and `scripts` have
  zero callers; `flag-embed-server/` was already absent). The Dependabot
  entries targeting it are removed with the directory (implementer C), and
  open Dependabot PRs #538–#541 are dispositioned **obsolete-by-removal**.
  `scripts/check_runtime_contract.py` treats its Dockerfile as an optional
  surface and skips it now that the file is gone.

## Why

Issue #258: the CI/integration/docs workstream closed the audit's frozen
check set C1–C25b — test theater that asserted nothing while production
contracts drifted (TEST-001's cap existed only in prose), build surfaces
whose version comments lied (BUILD-002), an installation guide that could
not be followed to a working install (DOC-001..003), and legacy obligations
(ENH-001/003/005/006/007/010/011, legacy-10/14) that were recorded but never
wired. Every repair is pinned by a mechanical check (frozen pytest nodes and
`scripts/check_*.py` gates in CI) so the classes cannot silently regress.

## Migration steps

- **Local dev Node floor is now 22.11** — `frontend/package.json` engines
  enforces `>=22.11.0` (CI, both Dockerfiles and CONTRIBUTING are aligned;
  `scripts/check_runtime_contract.py` fails any drift).
- Python stays 3.11 everywhere (CI, images, docs) — no action.
- **`backend/embedding_server/` removal**: the sidecar was dormant (no
  callers in `backend/app` or `scripts/`). Only custom deployments that
  independently built and ran it are affected; the supported alternative is
  the `harrier-embed` TEI service already defined in `docker-compose.yml`
  (which replaced flag-embed-server). Git history retains the directory if
  a fork needs it back.
- No database or API migration; the runtime contract and doc gates are
  additive CI checks.

## Breaking changes

None for supported deployments. Custom builds that wrapped the removed
embedding server must switch to the compose `harrier-embed` service (see
Migration).

## Known caveats

- **Nightly-tier results populate on first run**: the nightly workflow and
  the parser bake-off harness ship with their wiring; the committed
  benchmark markdown fills in when the first scheduled run executes (per the
  plan's assumption — the harness + wiring is the deliverable).
- The scanned-page fixture asserts zero-text behavior (a parser-evidence
  pin), so a future OCR feature will need to update that assertion
  deliberately.
- **E1-PR rebase coordination points** (recorded in the plan's impact
  analysis): `test_email_service.py` (E1 rewrote fakes; E2 changed the async
  base classes), `embeddings.py` (E1 moved breaker wrapping; E2's TEST-007
  rewrite mocks at the HTTP layer), and `backend/security/bandit-baseline.json`
  (both PRs shift lines — the second merger re-anchors via
  `python scripts/run_bandit.py --update-baseline`, the sanctioned
  procedure).
- AC20's caller sweep excludes `scripts/check_runtime_contract.py` by
  design — it names the removed Dockerfile as its optional surface and is
  the removal gate, not a caller.

## Acceptance evidence recorded in the PR body (not runtime checks)

- **AC14 — prior-failures triage**: full serial backend run at base a543361
  (Python 3.11, Windows) produced 23 failed + 37 errors / 6852 passed; every
  failure classified environmental — gold-corpus CRLF sha (5F + 37E),
  symlink privilege, POSIX traversal, lockfile network ×2, CSRF cookie-jar
  ×14, clock-sensitive TTL ×1 — with the raw log preserved in the issue
  trace (`repro/`). Nothing outside the environmental set.
- **AC23 — icon disposition (ENH-010)**: measured census — lucide in 110
  files, hugeicons in 5 files (NavigationRail, SessionRail, dialog.tsx,
  LoginPage, RegisterPage), overlap 4. Migration is cosmetic churn with
  visual-regression risk on nav identity icons; the issue bounds it "where
  practical without cosmetic churn" — dispositioned as a **documented
  deliberate split**, no code churn.
