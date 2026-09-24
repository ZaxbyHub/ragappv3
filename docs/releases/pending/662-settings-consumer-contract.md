# Settings-consumer contract: CI census proves every Settings field is read; four dormant fields removed (Issue #662)

## What changed

- New CI gate `scripts/check_settings_consumers.py`: an AST-based census that
  enumerates every `Settings` field from `backend/app/config.py`
  (pydantic-faithful: `AnnAssign` declarations only — plain assignments like
  `model_config` are not fields) and proves each has a production read site
  under `backend/app` + `scripts`, or fails the build naming the dormant
  field. Stdlib `ast` only; single pass, ~1-2 s.
- Census semantics (each rule verified against live code, and two of them were
  discovered the hard way during this issue's own reproduction):
  - a consumer is a Load-context read through any of the four binding forms
    this codebase uses: attribute read on a `settings` alias at ANY lexical
    scope (module-level or function-local), `getattr(<alias>, "field", ...)`
    with a constant string (multi-line calls included — the
    `contextual_chunking.py` shape single-line regex censuses miss),
    `<expr>.settings.<field>` chains (`self.settings.x`,
    `email_service.settings.x`; guarded to files that themselves import
    settings/Settings or call `get_settings`), and params annotated
    `: Settings` (FastAPI DI);
  - not consumption: whole-model serialization (`model_dump` never names
    fields), validators inside `config.py` (excluded from the scan scope),
    tests, the check scripts themselves, and textual presence — a field name
    in a comment, string literal, or Store-context write does not count;
  - relative imports (`from ..config import settings`) count, matching the
    two services that use that form.
- Escape hatch: `scripts/settings_dormant_allowlist.txt` (absent = empty).
  Entries carry `field :: reason :: owner-hint`; an entry without a reason or
  naming a non-existent field fails the check. No entries exist today — the
  tree is fully green with an empty allowlist.
- Wired into three enforcement surfaces (a check invoked nowhere guards
  nothing): the `quality-contracts` CI job (`.github/workflows/ci.yml`), the
  justfile `quality-contracts` recipe, and the pytest wrapper
  `backend/tests/test_settings_consumers_gate.py` (spawns the script; the
  Backend job's full-suite pytest makes that a fourth enforcement point). The
  wrapper also pins: the synthetic-dormant red path (exit 1, field named),
  the multi-line-getattr shape, AST-vs-textual-presence discrimination,
  allowlist accept/reject paths, `Settings.model_fields` enumeration parity,
  and the ci.yml/justfile wiring.
- Disposition of the four dormant fields the census found at HEAD (all
  remove-with-guard, each with verified evidence that no wire-able consumer
  path exists): `recency_decay_lambda` (the exponential-decay formula was
  never implemented — the wired `retrieval_recency_weight` blends recency
  with linear min-max normalization in `rag_engine.py`),
  `retrieval_profile` (superseded by the independently-wired
  `chunk_enrichment_enabled` boolean; no code path ever branched on the
  string — its default was even silently flipped `baseline`→`advanced` with
  no consumer to notice), `sparse_embedding_timeout` and
  `sparse_search_max_candidates` (belonged to the learned-sparse/
  FlagEmbedding path removed by the Harrier migration; the only remaining
  sparse surface is the schema-compat column in `vector_store.py`, which
  reads neither).
- Removal guard `backend/tests/test_issue662_unwired_internal_settings_removal.py`
  pins all four names absent from `Settings.model_fields` and from a
  `Settings()` instance (the #614/#625 precedent), so reintroduction requires
  a wiring decision, not an accident.
- `backend/tests/test_config_alignment.py` drops the default/env/validator
  assertions for the removed fields;
  `backend/tests/test_hybrid_logging.py` drops five inert mock assignments
  (`MagicMock` accepts any attribute — no behavior change).
- `docs/releases/pending/614-*.md` amended: its "deliberately retained"
  caveat is superseded by this change.

## Why

- The guard lattice checked the declaration side only
  (`check_config_contract.py` asserts defaults mirror across `.env.example`,
  compose, `config.py`, docs) — nothing asked whether a declared field is
  ever read, so a field could be declared, typed, defaulted, documented, and
  dead (#614 proved it; #625 fixed the two operator-facing instances with a
  one-shot removal guard and deliberately left these four "internal-only"
  knobs). This change makes the class unrepresentable: a declared-but-never-
  read field fails CI at authoring time.
- None of the four were exposed via the settings API (`SettingsUpdate`,
  `ALLOWED_FIELDS`, `SettingsResponse`, `_build_settings_dict`,
  `PERSISTED_FUNCTIONAL_FIELDS` contain none of the names), none appear in
  `.env.example`, `docker-compose.yml`, `docs/`, or `frontend/src`, and
  `Settings` uses `extra="ignore"`, so stale operator env vars drop silently
  — the earlier "retained for config compatibility" docstrings had no compat
  surface to protect.

## Migration steps

- Operators: nothing to do. The four fields were never settable through the
  API and never documented; env vars with these names (if anyone set them)
  are silently ignored, exactly as before.
- Backend developers: a new `Settings` field now ships together with a
  production read site, or with a reasoned `settings_dormant_allowlist.txt`
  entry. `python scripts/check_settings_consumers.py` (or the justfile
  `quality-contracts` recipe) reproduces the census locally; `--root` accepts
  a fixture tree.

## Known caveats

- Census residuals (both directions bounded, both deliberate):
  (A) a settings-importing file holding a non-Settings `.settings` object is
  counted as a consumer — this masks dormancy, the allowlist cannot remedy it
  (it suppresses flags, it cannot create them), detection is review-time;
  (B) a settings-agnostic file whose object holds a duck-typed Settings via
  an unannotated parameter (`self.settings = settings` from an unimported
  parameter) loses its chain reads — false dormancy, remedied by the
  allowlist, and the wrapper's real-tree test fails loud if a live field is
  ever caught.
- Dynamic-name `getattr(settings, var, ...)` access is not resolvable
  statically; the settings-API round-trip path is the known instance and is
  excluded as serialization. A future field consumed only that way needs an
  allowlist entry with a reason.
- `config.py` line numbers shifted by the removals; no bandit baseline re-anchor
  was needed (`run_bandit.py` reports no new findings — 131 suppressed, count
  unchanged; the removals shifted no baselined finding).
- Frontend `VITE_*` env vars are a different surface with different tooling
  and remain out of scope (per the issue).
