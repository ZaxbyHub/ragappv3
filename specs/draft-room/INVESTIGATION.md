# Draft Room — Feature Investigation

**Status:** Investigation only — no implementation. A `SPEC.md` should follow once the open questions below are decided.
**Branch:** `claude/rag-draft-room-investigation-hrasrz`
**Reference system:** [ZaxbyHub/opencode-newsroom](https://github.com/ZaxbyHub/opencode-newsroom) (read in full at commit `690fcea`)

---

## 1. The ask

A new sidebar section ("Draft Room") where the user uploads one or more documents and the system **rewrites them into high-quality prose, using the selected vault as a knowledge source**, with anti-AI-slop procedures modeled on the `opencode-newsroom` plugin — i.e., a real newsroom editorial pipeline: research → plan → write → copy edit → fact check → humanize, with hard quality gates between stages.

## 2. What the reference plugin actually does (verified from source)

The plugin is an OpenCode agent swarm. Its parts, and whether they translate to a server-side app feature:

| Plugin component | What it is | Translates to ragappv3 as |
|---|---|---|
| `editor_in_chief` (temp 0.3) | LLM orchestrator; never writes prose; classifies content into Tiers 1–3; enforces spec-first briefs | A **deterministic Python pipeline orchestrator** (see §5.1 — recommendation: do *not* port the LLM-orchestrator pattern) |
| `researcher` (temp 0.2) | Produces a structured research brief: key facts, statistics, expert positions, gaps/risks, source quality | Retrieval passes against the vault via the existing RAG engine, assembled into an "evidence pack" |
| `sme` (temp 0.3) | Per-domain expert guidance, one domain per call | Optional Phase-3 stage; vault + wiki/KMS evidence covers most of this need |
| `managing_editor` (temp 0.2) | Critic gate that reviews the *plan* before any writing | An LLM plan-review step with structured verdict (APPROVED / NEEDS_REVISION / REJECTED) |
| `writer` (temp 0.7) | One section at a time; carries the anti-slop writing rules (banned lexicon, burstiness, structure variation, concrete specifics) | The per-section generation call; port the prompt nearly verbatim |
| `copy_editor` (temp 0.2) | Dimension-scored review (style, grammar, clarity, flow, redundancy, tone) with line-level fixes; never rewrites | An LLM review step returning structured JSON verdicts |
| `fact_checker` (temp 0.1) | Claim-by-claim verification: VERIFIED / UNVERIFIABLE / INCORRECT / MISLEADING | The step where ragappv3 **exceeds** the plugin: real vault retrieval per claim + the existing citation validator |
| `humanizer` (temp 0.2) | Final AI-detection screen: perplexity, burstiness, transition overuse, hedging, structural repetition, AI vocabulary fingerprint; suggests targeted rewrites | An LLM review step; last gate before completion |
| **Stage A gates** (`pre_check_batch`) | **Deterministic** checks: 19-pattern banned-AI-vocab scan, sentence-length stats (avg + >30-word count), passive-voice density (regex, % of sentences), Flesch Reading Ease — each pass/warn/fail | Ports to ~200 lines of pure Python (`draft_quality.py`); unit-testable in CI with zero heavy deps |
| Workflow state machine | `idle → writer_delegated → copy_edit_run → fact_check_run → humanizer_run → complete`, strictly forward | Persisted stage field on the draft job |
| Guardrails circuit breaker | Tool-call/duration/loop/error limits; self-writing prevention; QA-skip enforcement | Bounded retry loops (`qa_retry_limit`), per-job wall-clock budget, max-section caps |
| `qa_retry_limit` | Bounded revision cycles per gate (user's config: 5) | A `draft_qa_retry_limit` setting |
| Tier classification | Tier 1 standard / Tier 2 high-stakes / Tier 3 legal-sensitive; Tier 3 requires human approval before publication | A per-draft `tier` field; Tier 3 ⇒ draft never auto-finalizes, requires explicit user acceptance |
| Heterogeneous models per role | Writer on one vendor, copy_editor/humanizer deliberately on a *different* vendor to catch model-specific tells; user runs three rosters (mega/paid/local incl. Ollama) | Phase 1: map roles onto the existing thinking/instant clients. Phase 2: per-role model settings (precedent: `wiki_llm_curator_*`) |
| Knowledge system (`knowledge.jsonl`) | Editorial lessons with category/confidence/quarantine, persisted across sessions | Phase 3; overlaps with existing memories/KMS |
| Evidence bundles | Per-section review evidence archived per task | `qa_report_json` per revision, rendered as a per-stage "trail" in the UI |
| `read-document` skill | docx/pdf/xlsx/pptx/csv/html → text via Python libs | Already superseded by the app's `unstructured`-based ingestion parser |

**The two-layer anti-slop design is the core insight to preserve:** the *writer prompt* prevents slop at generation time (banned lexicon + structural rules), and the *humanizer + deterministic pre-check* catch what slipped through. Both layers, plus bounded revision loops, are what make the plugin's output quality real rather than aspirational.

## 3. What ragappv3 already has (verified reuse map)

The app is unusually well prepared for this feature. Three findings shape the whole design:

1. **There are already three near-identical DB-backed background job systems** — `wiki_compile_jobs`, `kms_compile_jobs`, `document_reindex_jobs` — sharing one schema (`id, vault_id, trigger_type, trigger_id, status, error, result_json, input_json, retry_count, timestamps`) and one worker shape (`WikiCompileProcessor`: poll → claim → dispatch → complete/fail → auto-retry with backoff → publish SSE events). The Draft Room pipeline is a fourth instance of this pattern.
2. **The wiki curator is direct precedent for "LLM generates vault-grounded content in a background job"** — including its own optional dedicated model endpoint (`wiki_llm_curator_*` settings) and per-document compile triggered from ingestion.
3. **The generation-quality guardrails the plugin lacks already exist here**: hybrid retrieval + reranking, `[S#]/[M#]/[W#]/[K#]` citation contract, hallucinated-citation stripping and repair (`citation_validator`), per-claim citation-confidence scoring, prompt-injection boundaries, reasoning-trace sanitization. What the app *lacks* is exactly what the plugin has: the lexical anti-slop lint, the editorial role prompts, and the gated multi-pass writing loop. **The two systems are complementary with almost no overlap.**

Reuse anchors (verified in an independent review pass; see §9):

| Need | Existing anchor |
|---|---|
| Job table + worker | `backend/app/models/database.py` (`kms_compile_jobs` ~:708), `backend/app/services/wiki_compile_processor.py` (`_poll_loop` ~:89) |
| Upload + validation + parse | `backend/app/api/routes/documents.py` `_do_upload` (~:1676) → `BackgroundProcessor` → `DocumentProcessor` (unstructured parser, saves parsed text onto `files`) |
| Vault-scoped retrieval | `RAGEngine.query_retrieve_only` (~rag_engine.py:2822), `RAGEngine._execute_retrieval` (~:1861), `VectorStore.search` with `vault_id` filter |
| Cited prompt assembly | `PromptBuilderService.build_messages` (~prompt_builder.py:215) incl. `system_prompt_override`, wiki/KMS evidence, SECURITY BOUNDARY |
| LLM calls | `LLMClient.chat_completion(..., response_format=)` (~llm_client.py:199) / `chat_completion_stream` (~:285); thinking + instant clients on `app.state`; hot-reconfig via settings |
| Citation guardrails | `citation_validator.py`: `validate_and_repair_citations` (~:73), `score_citations` (~:379), `repair_against_sources_and_memories` (~:451) |
| Auth / vault RBAC | `deps.py` `evaluate(user, "vault", vault_id, action)` (~:728–808), `require_vault_permission()` (~:811) |
| Job progress → UI | `GET /documents/{id}/status` polling pattern + wiki fetch-SSE event stream (`useWikiEventStream.ts`) |
| Frontend page template | `DocumentsPage.tsx` (upload + `VaultSelector` + polling), wiki compile API shape (`wiki.ts` `compileDocumentWiki` → `{job_id,status}`) |
| Nav registration | `NavigationRail.tsx` `navItems` (~:40), `navigationTypes.ts` `NavItemId`, `MobileBottomNav.tsx` (separate list), `App.tsx` route + `getActiveItemFromPath` + `handleItemSelect` |
| Multi-pass LLM precedent | Query rewrite → step-back/HyDE → decomposition → CRAG evaluation → distillation (deterministically orchestrated); `AgenticPlanner` exists but ships disabled |

Notable gaps (nothing blocking, all decisions): no per-role model config beyond thinking/instant/wiki-curator; no lexical anti-slop lint anywhere in the backend; no existing draft/compose feature (clean namespace); citation labels have no space for "the uploaded source documents" (see §5.6).

## 4. Proposed design

### 4.1 UX flow

1. **Draft Room** appears in the `workspace` nav section. Landing page = list of drafts (status chips: Drafting / In review / Needs attention / Ready) + "New draft".
2. **New draft**: pick vault (reuse `VaultSelector`; the active vault is the default) → upload source document(s) (reuse `UploadDropzone`) → fill the **assignment brief**: piece type (rewrite / article / report / brief), target audience, tone/voice (free text, with examples), length target, content tier (1–3), and what to do on conflicts between the sources and the vault (prefer vault / prefer source / surface both).
3. **Pipeline view** (the "newsroom desk"): a stage tracker — Research → Plan → Write → Copy desk → Fact check → Humanize — driven by job SSE events + status polling, showing per-stage verdicts, retry counts, and warnings as they land.
4. **Draft detail**: rendered output with inline citations; a **QA report panel** (per-gate verdicts, deterministic-lint results, per-claim fact-check table with VERIFIED/UNVERIFIABLE/INCORRECT, citation-confidence score); revision history; side-by-side original-vs-rewrite for rewrite mode; actions: accept, re-run a stage, edit brief and recompile, export (`.md` first).

### 4.2 Data model (4 tables, mirroring existing conventions)

```
drafts             id, vault_id → vaults, title, brief_json, tier, status
                   ('assembling'|'queued'|'running'|'needs_review'|'ready'|'failed'|'archived'),
                   created_by → users, created_at, updated_at
draft_sources      draft_id → drafts, file_id → files   (sources are ordinary files rows)
draft_revisions    id, draft_id, revision_no, content_md, sections_json,
                   citations_json, qa_report_json, created_at
draft_jobs         verbatim clone of kms_compile_jobs columns
                   (+ stage TEXT for the newsroom state machine)
```

Added via an idempotent `migrate_add_draft_tables()` appended to the ordered list in `run_migrations()` — the established convention (no version table). FTS over `drafts`/`draft_revisions` can follow the `kms_entries_fts` trigger pattern later.

### 4.3 API surface (all under `/api/draft-room`, feature-gated like KMS)

```
POST   /draft-room/drafts                      create draft (brief + vault_id)         [vault write, csrf]
POST   /draft-room/drafts/{id}/sources         upload source file(s) → parse-only      [vault write, csrf]
POST   /draft-room/drafts/{id}/compile         enqueue draft_jobs row → {job_id}       [vault write, csrf, 202]
GET    /draft-room/drafts?vault_id=            list                                    [vault read]
GET    /draft-room/drafts/{id}                 detail incl. latest revision + QA report[vault read]
GET    /draft-room/drafts/{id}/status          job/stage progress (polling)            [vault read]
GET    /draft-room/events                      fetch-SSE job events (wiki-events shape)[vault read]
PUT    /draft-room/drafts/{id}                 edit brief / accept / archive           [vault write, csrf]
DELETE /draft-room/drafts/{id}                                                         [vault write, csrf]
```

Frontend: `lib/api/draftRoom.ts` on the shared `apiClient`; a `DraftRoomPage` modeled on `DocumentsPage`; nav updates in the four established places (`navigationTypes`, `NavigationRail`, `MobileBottomNav`, `App.tsx` ×3 spots).

### 4.4 The pipeline (a `DraftCompileProcessor`, pattern-copied from wiki)

Deterministic orchestrator; LLM only inside stages. Stage results append to the revision's `qa_report_json`; the `stage` column exposes live progress.

```
0 INTAKE      sources parsed (reuse ingestion parse machinery, parse-only mode →
              parsed text on the files row; no vault indexing — §5.2)
1 RESEARCH    LLM (instant): extract topics/claims from sources →
              per-facet vault retrieval (query_retrieve_only / _execute_retrieval)
              + wiki/KMS evidence → evidence pack with stable [S#]/[W#]/[K#] labels;
              flag source-vs-vault conflicts
2 PLAN        LLM (thinking): outline w/ per-section acceptance criteria, voice spec,
              word targets  →  CRITIC GATE (LLM, structured verdict; ≤N retries;
              REJECTED → job fails with reason → user edits brief)
3 WRITE       per section: LLM (thinking, temp ~0.7) with ported writer rules +
              section evidence + last ~2 ¶ of previous section for continuity
4 LINT        deterministic draft_quality.py — banned lexicon, burstiness (sentence-
              length variance), ¶-start transition counting, passive %, Flesch,
              same-opener runs; fail → targeted rewrite with flagged passages (≤2)
5 COPY DESK   LLM (structured JSON verdict per dimension) → writer revision loop
              (≤ draft_qa_retry_limit)
6 FACT CHECK  claim extraction → per-claim vault re-retrieval → VERIFIED/UNVERIFIABLE/
              INCORRECT/MISLEADING + validate_and_repair_citations + score_citations;
              INCORRECT → targeted revision; UNVERIFIABLE → kept but flagged in QA report
7 HUMANIZE    LLM (ported humanizer prompt) → targeted rewrites → re-run LINT
8 ASSEMBLE    merge, final citation repair, sanitize, store draft_revisions row,
              status → ready (Tiers 2–3 → needs_review), SSE 'done'
```

Budget rails replacing the plugin's circuit breaker: per-stage retry caps, per-job wall-clock budget, max-section cap, and a hard cap on total LLM calls per job.

### 4.5 Anti-slop procedures, concretely

- **`draft_quality.py`** (new, pure Python): port `pre_check_batch` heuristics + add burstiness variance, paragraph-opener repetition, transition-word-at-¶-start counts, "AI triad" detection. Banned lexicon = union of the plugin's writer + pre-check + humanizer lists, stored as a configurable setting so it can evolve without deploys. Fully unit-testable under CI's reduced dependency set.
- **Role prompts** ported from the plugin (writer/copy-editor/fact-checker/humanizer/managing-editor), adapted to cite vault evidence. Candidate home: the existing `prompt_versions` infrastructure (versioning + org overrides) rather than new hardcoded strings — decision point.
- **Grounding as a first-class gate** (the app's edge over the plugin): every stage-6 claim verdict is backed by actual retrieval; final output runs through citation repair + confidence scoring; unverifiable claims surface in the QA panel instead of silently shipping.
- **Structured verdicts everywhere**: gates return JSON (via `response_format`) so retry loops branch on data, not on parsing prose.

### 4.6 Models

Phase 1 maps roles onto what exists: thinking client = writer/plan/copy/fact-check/humanize; instant client = research facet extraction and cheap classification. Phase 2 adds `draft_<role>_url/_model` settings (wiki-curator precedent, hot-reconfig included) to honor the plugin's cross-vendor blindspot strategy — the user already operates heterogeneous rosters (mega/paid/local) and will want this.

## 5. Key design decisions

**5.1 Deterministic orchestrator, not an LLM editor-in-chief.** The plugin needs an LLM orchestrator because OpenCode is a chat harness. A server app should encode the pipeline as code: testable, resumable, cheaper, and no guardrails needed against the conductor going rogue (the plugin spends real machinery preventing self-writing and delegation loops — problems that don't exist when the conductor is Python). This also matches the app's own philosophy: its multi-pass RAG pipeline is deterministic orchestration; `AgenticPlanner` exists but ships disabled.

**5.2 Draft sources are parse-only — not indexed into the vault (recommended).** Uploading sources through the normal path would make them retrievable, so the rewrite would "ground" itself in its own source and pollute chat/wiki for every vault user. Recommended: reuse the upload+parse machinery with an indexing-skip flag (parse → text persisted on the `files` row → terminal state without LanceDB writes), keeping validation/progress for free. A "promote to vault" action can index a source or finished draft later, deliberately.

**5.3 Rewrite vs compose are both real, and they differ.** "Rewrite" preserves the source's structure and claims, improving prose and grounding checks. "Compose" (the newsroom mode) plans its own structure from sources + vault. The brief's piece type selects between them; rewrite mode can skip PLAN (structure comes from the source) making it the cheaper MVP path.

**5.4 Permissions: vault `write` to create/compile, `read` to view (recommended).** Matches wiki/KMS compile semantics and keeps viewer roles read-only. Alternative (drafts as personal workspace requiring only `read`) is defensible but inconsistent with siblings; flag for decision.

**5.5 Latency is a feature constraint, not an afterthought.** On local models, a 6-section Tier-1 piece is roughly 25–45 LLM calls with retries — tens of minutes. Hence: background job + SSE (never a blocking request), per-job budgets, stage-level progress, and a "needs_review" landing state rather than a spinner. Rewrite mode on a short doc should still feel fast (few sections, PLAN skipped).

**5.6 Citation label space needs one addition.** Rewritten prose draws on two evidence classes: the uploaded sources and the vault. Proposal: keep `[S#]/[W#]/[K#]` for vault evidence and add `[D#]` for source documents, so the fact-checker can distinguish "claim came from the source doc (preserved)" from "claim added and vault-grounded" — the QA panel then shows exactly what the rewrite introduced. Requires a small extension to the citation validator's label registry.

## 6. Phased plan (each phase shippable)

- **Phase 1 — MVP rewrite loop.** Tables + migration; parse-only source upload; `DraftCompileProcessor` with INTAKE → RESEARCH → WRITE (single pass) → LINT → FACT-CHECK-lite (citation validation + repair) → ASSEMBLE; `draft_quality.py` + tests; routes; nav + `DraftRoomPage` (list, brief form, progress, output + basic QA panel). *Backend ~1.5–2k LOC, frontend ~1–1.5k LOC.*
- **Phase 2 — Full newsroom.** PLAN + critic gate; copy desk + humanizer as distinct gated loops; tiers; revision history UI; per-role model settings; SSE event stream; side-by-side rewrite view; `.md`/`.docx` export.
- **Phase 3 — Newsroom memory & polish.** Editorial knowledge (lessons/style notes — possibly as a KMS category); voice samples; per-section re-runs; SME stage; slop-metric eval harness alongside `/eval`; promote-to-vault/wiki.

## 7. Risks

1. **Pipeline cost/latency on local models** — bounded by budgets and model tiering, but a real UX risk; surface projected stage counts in the UI before compile.
2. **CI reduced dependency set** — new services touching `lancedb`/`unstructured` need the established per-file import stubs; keep `draft_quality.py` dependency-free.
3. **Gate loops that never converge** — the plugin caps retries for a reason; every loop needs a cap and a "flagged but shipped" degradation path recorded in the QA report.
4. **Vault-empty grounding** — when retrieval returns nothing relevant, degrade to source-only rewrite with an explicit "no vault grounding" banner (CRAG evaluator reuse) rather than hallucinated citations.
5. **Prompt drift across roles** — centralize role prompts (ideally versioned in `prompt_versions`) instead of scattering string constants.
6. **Nav/mobile duplication** — `MobileBottomNav` maintains a separate item list; easy to miss.

## 8. Open questions (blocking a SPEC)

1. Source handling default: parse-only (recommended) vs index-into-vault? Should "promote to vault" exist in Phase 1?
2. Rewrite-first MVP (recommended) or compose-first?
3. Create/compile permission: vault `write` (recommended) or `read`?
4. Per-role models in Phase 1 or Phase 2? (Phase 2 recommended; thinking/instant split is serviceable initially.)
5. Export targets: `.md` only for MVP, or is `.docx` required day one?
6. Should Tier 2/3 semantics (mandatory human review before `ready`) be in Phase 1?
7. Any appetite for the deterministic lint (`draft_quality.py`) to also run on *chat* answers later? It's independently useful.

## 9. Verification note

Plugin claims: verified first-hand (all agent prompts, guardrails, pre-check tool, state machine, config read at commit `690fcea`). ragappv3 claims: mapped by three parallel explorer passes, then re-verified by an independent reviewer pass that opened every cited file — all 22 load-bearing claims confirmed. Two precision notes from that pass: the `files.parsed_text` persistence is at `document_processor.py:2988–2991`, and `wiki_compile_jobs` lacks the `input_json`/`retry_count` columns that `kms_compile_jobs` has (hence §4.2 clones the **kms** table shape). Anchors above use `~` where line numbers may drift.
