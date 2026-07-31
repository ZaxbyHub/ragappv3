# Draft Room — Feature Investigation

**Status:** Investigation closed — no implementation. [`SPEC.md`](./SPEC.md) is the normative implementation contract.
**Branch:** `claude/rag-draft-room-investigation-hrasrz`
**Reference system:** [ZaxbyHub/opencode-newsroom](https://github.com/ZaxbyHub/opencode-newsroom) (read in full at commit `690fcea`)
**Revision 3:** reconciled with an independent second-opinion review (GPT 5.6 Sol) and the four final implementation corrections: fact-last semantics, dedicated draft-input storage, a narrow boilerplate hard gate, and honest lexical-overlap terminology.

---

## 1. The ask

A new sidebar section ("Draft Room") where the user uploads one or more documents and the system **rewrites them into high-quality prose, using the selected vault as a knowledge source**, with anti-AI-slop procedures modeled on the `opencode-newsroom` plugin — i.e., a real newsroom editorial pipeline: research → plan → write → copy edit → standards review → fact verification → human review, with hard quality gates between stages.

Both independent investigations reached the same verdict: **worth building, and "Draft Room" is the right name** (it covers reports, briefs, docs, and press releases — not just journalism).

## 2. What the reference plugin actually does (verified from source)

The plugin is an OpenCode agent swarm. Its parts, and whether they translate to a server-side app feature:

| Plugin component | What it is | Translates to ragappv3 as |
|---|---|---|
| `editor_in_chief` (temp 0.3) | LLM orchestrator; never writes prose; classifies content into Tiers 1–3; enforces spec-first briefs | A **deterministic Python pipeline orchestrator** (see §5.1 — recommendation: do *not* port the LLM-orchestrator pattern) |
| `researcher` (temp 0.2) | Produces a structured research brief: key facts, statistics, expert positions, gaps/risks, source quality | Retrieval passes against the vault via the existing RAG engine, assembled into an "evidence pack" |
| `sme` (temp 0.3) | Per-domain expert guidance, one domain per call | Optional Phase-3 stage; vault + wiki/KMS evidence covers most of this need |
| `managing_editor` (temp 0.2) | Critic gate that reviews the *plan* before any writing | An LLM plan-review step with structured verdict (APPROVED / NEEDS_REVISION / REJECTED) |
| `writer` (temp 0.7) | One section at a time; carries anti-slop writing rules (lexicon, rhythm, structure variation, concrete specifics) | The per-section generation call; adapt its positive editorial rules, but do not copy detector-evasion framing or turn the plugin's full vocabulary list into a hard gate |
| `copy_editor` (temp 0.2) | Dimension-scored review (style, grammar, clarity, flow, redundancy, tone) with line-level fixes; never rewrites | An LLM review step returning structured JSON verdicts |
| `fact_checker` (temp 0.1) | Claim-by-claim verification: VERIFIED / UNVERIFIABLE / INCORRECT / MISLEADING | The step where ragappv3 **exceeds** the plugin: real vault retrieval per claim + a strengthened claim ledger (§4.5) |
| `humanizer` (temp 0.2) | Final screen framed as *AI-detector evasion* (GPTZero/Originality/Turnitin), checking perplexity, burstiness, transition overuse, hedging, structural repetition, AI vocabulary | **Reframed as a "standards desk"** (§5.7): keep every underlying quality check, drop the detector-evasion objective |
| **Stage A gates** (`pre_check_batch`) | **Deterministic** checks: 19-pattern banned-AI-vocab scan, sentence-length stats, passive-voice density, Flesch Reading Ease — each pass/warn/fail | Ports to a small pure-Python `draft_quality.py`; only an exact, curated multi-word boilerplate list is blocking. Broad vocabulary and statistical heuristics are advisory, with quote/code/citation/locked-text exclusions and recorded waivers (§5.8) |
| Workflow state machine | `idle → writer_delegated → copy_edit_run → fact_check_run → humanizer_run → complete`, strictly forward | Persisted stage field on the draft job |
| Guardrails circuit breaker | Tool-call/duration/loop/error limits; self-writing prevention; QA-skip enforcement | Bounded retry loops (`qa_retry_limit`), per-job wall-clock budget, max-section caps |
| Tier classification | Tier 1 standard / Tier 2 high-stakes / Tier 3 legal-sensitive; Tier 3 requires human approval | A per-draft `tier` field driving gate strictness (human approval is universal — §5.5) |
| Heterogeneous models per role | Writer on one vendor, reviewers deliberately on another; user runs three rosters (mega/paid/local incl. Ollama) | Phase 1: the app's configured thinking/instant clients. Phase 2/3: optional per-role settings (precedent: `wiki_llm_curator_*`) |
| Knowledge / evidence / output managers | Editorial lessons (JSONL w/ quarantine), per-task evidence bundles, output store | **Design-only in the plugin**: verified that `saveEvidence`/`addKnowledge` are exported but never called by any runtime path (only `loadEvidence`/reads are wired). Treat the plugin as a workflow-design source, not an engine — both reviews independently reached this conclusion |
| `read-document` skill | docx/pdf/xlsx/pptx/csv/html → text via Python libs | Already superseded by the app's `unstructured`-based ingestion parser |

**The two-layer anti-slop design is the core insight to preserve:** the *writer prompt* prevents stock prose at generation time (positive voice and structural rules), and the *review gates + deterministic lint* catch what slipped through — with bounded revision loops so the pipeline always terminates.

## 3. What ragappv3 already has (verified reuse map)

Three findings shape the whole design:

1. **There are already three near-identical DB-backed background job systems** — `wiki_compile_jobs`, `kms_compile_jobs`, `document_reindex_jobs` — sharing one schema and one worker shape (`WikiCompileProcessor`: poll → claim → dispatch → complete/fail → auto-retry with backoff → publish SSE events). The Draft Room pipeline is a fourth instance of this pattern, which (unlike the in-memory ingestion queue) survives restarts and supports cancel/retry — essential for multi-minute editorial jobs.
2. **The wiki curator is direct precedent for "LLM generates vault-grounded content in a background job"** — including its own optional dedicated model endpoint (`wiki_llm_curator_*`) and per-document compile triggered from ingestion. The wiki subsystem also already **normalizes claims and claim–source links** (`wiki_claims`, `wiki_claim_sources`) — repo precedent for the Phase-2 claim ledger (§4.2).
3. **The generation-quality guardrails the plugin lacks already exist here**: hybrid retrieval + reranking, `[S#]/[M#]/[W#]/[K#]` citation contract, hallucinated-citation stripping and repair (`citation_validator`), per-citation lexical-overlap scoring, prompt-injection boundaries, reasoning-trace sanitization. Lexical overlap is a retrieval-integrity hint, not claim support or entailment; the separate atomic-claim ledger supplies those verdicts. What the app *lacks* is exactly what the plugin has: the lexical anti-slop lint, the editorial role prompts, and the gated multi-pass writing loop. **The two systems are complementary with almost no overlap.**

Reuse anchors (all verified by an independent reviewer pass; see §9):

| Need | Existing anchor |
|---|---|
| Job table + worker | `backend/app/models/database.py` (`kms_compile_jobs` ~:708), `backend/app/services/wiki_compile_processor.py` (`_poll_loop` ~:89) |
| Upload + validation + parse | `backend/app/api/routes/documents.py` `_do_upload` (~:1676) → `BackgroundProcessor` → `DocumentProcessor` (unstructured parser; parsed text persisted at ~:2988) |
| Vault-scoped retrieval | `RAGEngine.query_retrieve_only` (~rag_engine.py:2822), `RAGEngine._execute_retrieval` (~:1861), `VectorStore.search` with `vault_id` filter |
| Cited prompt assembly | `PromptBuilderService.build_messages` (~prompt_builder.py:215) incl. `system_prompt_override`, wiki/KMS evidence, SECURITY BOUNDARY |
| LLM calls | `LLMClient.chat_completion(..., response_format=)` (~llm_client.py:199) / `chat_completion_stream` (~:285); thinking + instant clients on `app.state`; hot-reconfig via settings |
| Citation guardrails | `citation_validator.py`: `validate_and_repair_citations` (~:73), `score_citations` (~:379), `repair_against_sources_and_memories` (~:451). **Known limit:** label integrity + lexical overlap, not entailment (§4.5) |
| Auth / vault RBAC | `deps.py` `evaluate(user, "vault", vault_id, action)` (~:728–808), `require_vault_permission()` (~:811) |
| Job progress → UI | `GET /documents/{id}/status` polling pattern + wiki fetch-SSE event stream (`useWikiEventStream.ts`) |
| Frontend page template | `DocumentsPage.tsx` (upload + `VaultSelector` + polling), wiki compile API shape (`wiki.ts` `compileDocumentWiki` → `{job_id,status}`) |
| Nav registration | `NavigationRail.tsx` `navItems` (~:40), `navigationTypes.ts` `NavItemId`, `MobileBottomNav.tsx` (separate list), `App.tsx` route + `getActiveItemFromPath` + `handleItemSelect` |
| Multi-pass LLM precedent | Query rewrite → step-back/HyDE → decomposition → CRAG evaluation → distillation (deterministically orchestrated); `AgenticPlanner` exists but ships disabled |

Notable gaps (all decisions, none blocking): no per-role model config beyond thinking/instant/wiki-curator; no lexical anti-slop lint anywhere in the backend (verified absence); no existing draft/compose feature (clean namespace); citation labels have no space for uploaded source documents (§5.6); **no diff/editor component exists in the frontend** (verified: no monaco/codemirror/diff library anywhere) — the side-by-side original/rewrite view is real new UI work.

## 4. Proposed design

### 4.1 UX flow — a project workspace, not a chat transcript

1. **Draft Room** appears in the `workspace` nav section. Landing page = list of draft projects (status chips) + "New draft".
2. **New draft**: pick vault (reuse `VaultSelector`; active vault as default) → upload source document(s) through a purpose-built `DraftInputDropzone` that reuses document-upload visuals/validation but not its ingestion store → fill the **assignment brief**:
   - mode (rewrite / compose), output piece type (article / report / brief / press release / other), target audience, tone/voice, length target, content tier (1–3);
   - **a role for every uploaded source**: `manuscript` (preserve & improve) / `reference` (candidate factual evidence, still verified) / `style exemplar` (imitate voice, never trust content) / `background` (summarizable) / `challenge` (claims to be verified against the vault, not trusted). Role controls use, not authority or truth;
   - conflict policy (prefer vault / prefer source / surface both).
3. **Pipeline view** (the stage rail): Assignment → Research → Outline → Draft → Copy desk → Standards → Fact desk → Human review, driven by job SSE events + status polling, showing per-stage verdicts, retry counts, and warnings. **Every stage produces an inspectable artifact** — research packet, approved outline, per-section drafts, claim ledger, findings — never a silent jump from sources to polished prose.
4. **Draft detail**: three-pane workspace — inputs/brief on the left, editor with original/rewrite/diff views + version history in the center, evidence on the right (sources, claims with statuses, contradictions, findings, unresolved warnings). Actions: accept/reject findings, re-run a stage, edit brief and recompile, mark **Ready** (human-only), export (`.md` first).

### 4.2 Data model (MVP: 4 tables; Phase 2: normalized claim ledger)

```
drafts             id, vault_id → vaults, title, brief_json (incl. per-source roles),
                   tier, status ('draft'|'queued'|'running'|'needs_review'|
                   'ready'|'failed'|'archived'), created_by → users, created_at, updated_at
draft_inputs       id, draft_id → drafts, original_name, media_type, byte_size,
                   storage_path, raw_sha256, parsed_text, parsed_sha256, parse_status,
                   role ('manuscript'|'reference'|'style'|'background'|'challenge')
                   — dedicated project-private storage; never an ordinary `files` row
draft_revisions    id, draft_id, revision_no, content_md, sections_json,
                   citations_json, qa_report_json, created_at
draft_jobs         clone of kms_compile_jobs columns (+ stage TEXT)
```

Added via an idempotent `migrate_add_draft_tables()` appended to the ordered list in `run_migrations()` — the established convention. **Phase 2** normalizes the QA blob into `draft_claims` / `draft_claim_sources` / `draft_findings` (mirroring the existing `wiki_claims` / `wiki_claim_sources` precedent) so the UI can do per-claim accept/reject and the ledger is queryable; `qa_report_json` is sufficient for the MVP but is not the end state. Approvals can ride a small `draft_events` audit table (who approved, when, from which revision).

### 4.3 API surface (all under `/api/draft-room`; navigation remains hidden until the full factuality gates ship)

```
POST   /draft-room/drafts                      create draft (brief + vault_id)         [vault read, csrf]
POST   /draft-room/drafts/{id}/inputs          upload project-private input(s) → parse [owner + vault read, csrf]
POST   /draft-room/drafts/{id}/compile         enqueue draft_jobs row → {job_id}       [owner + vault read, csrf, 202]
GET    /draft-room/drafts?vault_id=            list own metadata + vault access         [owner]
GET    /draft-room/drafts/{id}                 detail metadata (large artifacts paged)  [owner + vault read]
GET    /draft-room/drafts/{id}/jobs/{job_id}   canonical job/stage progress             [owner + vault read]
GET    /draft-room/drafts/{id}/events          fetch-SSE job events (wiki-events shape)[owner + vault read]
PATCH  /draft-room/drafts/{id}                 edit title/brief/tier                    [owner + vault read, csrf]
POST   /draft-room/drafts/{id}/archive         archive                                 [owner + vault read, csrf]
POST   /draft-room/drafts/{id}/revisions/{rid}/ready  human-only Ready                 [owner + vault read, csrf]
POST   /draft-room/drafts/{id}/promote         index result/source into vault          [owner + vault write, csrf]
DELETE /draft-room/drafts/{id}                                                         [owner, csrf]
```

Permissions model (§5.4): drafts are **user-owned, project-private**; vault `read` is what compiling requires (it only *reads* the vault, like chat); vault `write` is required only to promote/publish anything back into shared space.

Frontend: `lib/api/draft-room.ts` on the shared `apiClient`; a `DraftRoomPage` modeled on `DocumentsPage` (reuse visual primitives and the three-pane layout *pattern* from `ChatShell`, but **dedicated stores** — draft state is not chat state; do not reuse `useChatStore`); nav updates in the four established places.

### 4.4 The pipeline (a `DraftJobProcessor`, pattern-copied from wiki and dispatching parse/compile jobs)

Deterministic orchestrator; LLM only inside stages ("logical roles, not literal agents" — separation comes from distinct prompts, restricted inputs, and versioned outputs). Stage results persist so the UI shows the trail.

```
0 INTAKE      inputs parsed by a shared extraction service into dedicated
              `draft_inputs`; no ordinary `files` row and no vault indexing (§5.2)
1 RESEARCH    LLM (instant): extract topics/claims from sources honoring source roles →
              per-facet vault retrieval through a new public source-returning RAG method
              + wiki/KMS evidence → research packet with stable [D#]/[S#]/[W#]/[K#]
              labels + contradiction map (source-vs-vault, source-vs-source)
2 PLAN        LLM (thinking): outline w/ per-section acceptance criteria, voice spec,
              word targets  →  CRITIC GATE (LLM, structured verdict; ≤N retries;
              REJECTED → job fails with reason → user edits brief).
              Rewrite mode: outline is derived from the manuscript's own structure
3 WRITE       per section: LLM (thinking, temp ~0.5) with adapted writer rules +
              section evidence + last ~2 ¶ of previous section for continuity
4 LINT        deterministic draft_quality.py — exact curated boilerplate phrases
              are blocking; broad vocabulary and statistical signals are advisory;
              exclusions protect quotes, code, citations, and locked text; a block
              triggers a targeted rewrite (≤2), then an explicit human waiver/finding
5 COPY DESK   LLM (structured JSON verdict per dimension) → targeted revision loop
              (≤ draft_qa_retry_limit)
6 STANDARDS   LLM (reframed humanizer, §5.7): stock phrasing, uniform rhythm, hedging,
              inflated significance, lost nuance/uncertainty, silent fact removal,
              style divergence from exemplars → targeted rewrites → bounded
              LINT → COPY → STANDARDS convergence, then FACT
7 FACT DESK   the final semantic gate. Atomic-claim ledger (§4.5): per-claim vault
              re-retrieval → Supported / Contradicted / Ambiguous / Stale /
              Unsupported / Opinion. Evidence-bearing verdicts store exact passages;
              Unsupported stores the query/scope and zero-result or nearest context;
              quote-fidelity gate (exact or marked paraphrase). The fact desk never
              silently rewrites. Corrections return through COPY → STANDARDS → FACT;
              any semantic edit after FACT invalidates its verdict and must re-enter FACT
8 ASSEMBLE    validate and package the Fact candidate byte-for-byte; citation repair
              and trace sanitation must already have run before Fact. Store revision,
              status → needs_review. ONLY A HUMAN sets 'ready' (§5.5), SSE 'done'
```

Budget rails replacing the plugin's circuit breaker: per-stage retry caps, per-job wall-clock budget, max-section cap, hard cap on total LLM calls per job.

### 4.5 Anti-slop and factuality, concretely

**"Anti-AI-slop" is a positive editorial standard, not detector evasion.** AI-writing detectors are unreliable in both directions ([OpenAI retired its own classifier](https://openai.com/index/new-ai-classifier-for-indicating-ai-written-text/) after reporting a 26% true-positive / 9% false-positive rate; [NIST's GenAI program](https://ai-challenges.nist.gov/genai) reports that three pilot generators fooled every tested detector), so "passes GPTZero" is the wrong optimization target. The right target is the checklist real desks use: unsupported facts/numbers/names/causal claims, altered or invented quotes, vague attribution ("experts say"), stock openings and canned conclusions, inflated significance, mechanically uniform rhythm and repeated sentence templates, lost nuance/uncertainty/limitations, silent removal of material facts, style divergence from approved exemplars. This mirrors AP's published standards: [treat generative output as unvetted source material](https://www.ap.org/the-definitive-source/behind-the-news/standards-around-generative-ai/) and require [accurate, precise quotations](https://www.ap.org/about/news-values-and-principles/news-values-introduction/).

- **`draft_quality.py`** (new, pure Python, CI-testable): implement two rule classes, not one indiscriminate lexicon. `blocked_boilerplate` is a short configurable list of exact multi-word stock constructions and is the only automatic hard gate. `review_vocabulary` (single words such as "delve" or "tapestry"), burstiness, opener repetition, transition counts, passive %, Flesch, and triad patterns are advisory findings. Mask direct quotes, Markdown blockquotes, fenced code, citations, and explicitly locked spans before matching. After two targeted rewrites, a remaining boilerplate match becomes a visible, auditable human-waivable blocker rather than an infinite loop.
- **The fact desk needs more than today's citation validator.** `citation_validator.py` verifies *label integrity* and calculates per-citation *lexical overlap* — a valid `[S3]` label does not prove the cited passage entails the claim. The fact desk therefore decomposes output into atomic claims (FActScore-style) and re-retrieves per claim, recording the exact supporting passage + document identity + chunk reference for each Supported verdict. The existing validator remains the final label-integrity pass.
- **Role prompts** adapted from the plugin (writer/copy-editor/fact-checker/managing-editor; standards desk rewritten per §5.7) and versioned in a Draft Room prompt module. Do not overload the current globally selected chat prompt behavior unless it is first generalized safely.
- **Structured verdicts everywhere** (via `response_format`) so retry loops branch on data, not prose parsing.

### 4.6 Models

Phase 1 uses the configured clients: thinking = plan/write/copy/fact/standards; instant = research facet extraction and cheap classification. Phase 2/3 adds optional `draft_<role>_url/_model` settings (wiki-curator precedent, hot-reconfig included). The plugin's cross-vendor blindspot strategy stays available to those who run multiple endpoints — this user demonstrably does (three rosters incl. local Ollama) — but is never required: single-model deployments must work fully.

## 5. Key design decisions

**5.1 Deterministic orchestrator, not an LLM editor-in-chief.** The plugin needs an LLM orchestrator because OpenCode is a chat harness — and then spends real machinery policing it (self-writing detection, delegation-loop guards, circuit breakers). A server app encodes the pipeline as code: testable, resumable, cheaper, and those failure modes vanish. Matches the app's own philosophy (multi-pass RAG is deterministic orchestration; `AgenticPlanner` ships disabled). Both reviews independently reached this conclusion.

**5.2 Draft inputs are project-private and parse-only — never ordinary vault `files` rows and never auto-indexed.** Otherwise the rewrite can retrieve its own manuscript as independent "grounding," and drafts-in-progress can leak into document, chat, or wiki surfaces. Extract shared file-validation and parsing functions from ingestion, but persist raw bytes and parsed text only in dedicated `draft_inputs` storage. A deliberate **promote** action (vault `write`) creates a new ordinary file or finished-draft document later; promotion is a copy with provenance, not a flag flip. Both reviews converged on isolation.

**5.3 Rewrite vs compose are both real modes.** Rewrite preserves the manuscript's structure and claims (outline derived, critic gate trivial) — the cheaper, faster path. Compose plans its own structure from sources + vault. The draft's `mode` selects the operation independently of the brief's output `piece_type`; MVP leads with rewrite while keeping the research-packet and outline artifacts visible in both modes.

**5.4 Permissions: vault `read` → private drafts; vault `write` → promote/share/publish.** *(Revised from this document's first version, which recommended `write` to create.)* The wiki/KMS-compile analogy was the wrong one: those jobs mutate shared vault knowledge, while draft compilation only *reads* the vault — exactly like chat, which requires `read`. Viewer-role users get a private drafting workspace; nothing touches shared space without `write`. Export of vault-derived drafts should be audit-logged (§7).

**5.5 A human marks Ready — always.** Pipeline completion lands at `needs_review` for every tier; tiers modulate gate strictness, not the human sign-off. If a gate hasn't run (e.g., Phase 1 before the claim ledger exists), the UI must say so honestly: "Draft generated — not fact-checked," never an implicit "Ready."

**5.6 Citation label space gets one addition: `[D#]` for uploaded source documents,** distinct from vault `[S#]/[W#]/[K#]`, so the fact desk can distinguish "preserved from your manuscript" from "added and vault-grounded," and the QA panel shows exactly what the rewrite introduced. Small extension to the validator's label registry.

**5.7 Reframe the humanizer as a standards desk.** Its underlying checks (burstiness, transition overuse, hedging, structural repetition, stock vocabulary) are legitimate editorial lint and stay. Its *stated objective* — make text pass AI detectors — goes: it optimizes the wrong target (detectors are unreliable), and detector evasion as a product goal invites misuse this app doesn't need. The standards desk also picks up the checks detectors can't do: lost nuance, silent fact removal, unearned certainty, style divergence from exemplars.

**5.8 Deterministic lint is a gate only where it is precise.** A curated exact multi-word boilerplate list can block automatic completion. Broad single-word vocabulary (including "delve" and "tapestry"), passive-voice %, Flesch, sentence statistics, transition counts, and rhythm measures are advisory because context determines whether they are defects. Quote/code/citation/locked-text exclusions are mandatory. Every remaining hard match names the rule and span; after bounded rewrites it requires a recorded human waiver, never silent removal or an endless loop.

**5.9 Latency is a feature constraint.** A full-pipeline piece on local models is 25–45+ LLM calls — tens of minutes. Hence: durable background jobs (wiki-job pattern, restart/cancel/retry-safe), stage-level progress, projected stage counts shown before compile, section-level re-runs, and stage caching. Rewrite mode on a short doc should still feel fast.

## 6. Second-opinion reconciliation

An independent review (GPT 5.6 Sol) examined the same plugin and codebase. Convergent conclusions — now high-confidence: build it; name it Draft Room; deterministic orchestration with logical roles; project-private parse-only sources; wiki-job pattern over the ingestion queue; dedicated stores/routes rather than chat reuse; durable jobs + SSE; per-stage inspectable artifacts; the plugin is a workflow-design source, not an engine.

The four final review findings were validated against revision 2 and current source before revision 3 changed them:

| Finding | Classification | Evidence and decision |
|---|---|---|
| Standards rewrites occurred after Fact | **CONFIRMED** | Revision 2 placed Fact at stage 6 and Standards at stage 7 while allowing targeted Standards rewrites. Revision 3 makes Fact last and routes every semantic correction through Copy → Standards → Fact. |
| Parse-only inputs could still be ordinary `files` rows | **CONFIRMED** | Revision 2 explicitly stored parsed text on a `files` row and linked `draft_sources.file_id`. Revision 3 requires dedicated `draft_inputs` bytes/text and a later copy-with-provenance promote operation. |
| The lexicon hard gate was too broad | **PARTIALLY_VALID** | A deterministic hard gate remains valuable, but blocking the union of broad single-word lists is not precise. Only curated exact multi-word boilerplate blocks; vocabulary/statistics are advisory with exclusions and auditable waivers. |
| “Per-claim citation confidence” overstated the current scorer | **CONFIRMED** | `citation_validator.py` computes Jaccard token overlap per citation label. Revision 3 calls this per-citation lexical overlap and keeps claim support in the separate atomic ledger. |

Adopted from that review into this revision: per-source roles in the brief (§4.1); the standards-desk reframe replacing detector-evasion (§5.7); the atomic-claim ledger with entailment-oriented verdicts and quote-fidelity gate (§4.5, §4.4 stage 7); the invariant that FACT is the last semantic gate and every later semantic change must re-enter it; the permissions revision (§5.4); universal human Ready (§5.5); honest gate labeling; Phase 0 gold corpus (§8); export auditing, provider-content policy, and the editor-gap risk (§7).

Verified rather than assumed from that review: the plugin's evidence/knowledge write paths are indeed unwired (grep: `saveEvidence`/`addKnowledge` exported, zero callers); the frontend indeed has no diff/editor component; `citation_validator` is indeed label-integrity + lexical overlap.

Retained in narrower form after that review's skepticism: an exact multi-word boilerplate **hard** gate (§5.8), while vocabulary and statistical signals remain advisory. Also retained is optional per-role model routing in later phases (§4.6 — this user demonstrably operates heterogeneous model rosters; the *default* remains the app's configured models, where both reviews agree).

## 7. Risks

1. **False confidence** — polished prose with valid-looking labels can still be unsupported; the claim ledger + honest gate labeling exist precisely for this.
2. **Pipeline cost/latency on local models** — bounded by budgets, tiering, caching, section re-runs (§5.9).
3. **CI reduced dependency set** — services touching `lancedb`/`unstructured` need the established per-file import stubs; keep `draft_quality.py` dependency-free.
4. **Gate loops that never converge** — every loop capped, with a "flagged but shipped as finding" degradation path recorded in the QA report.
5. **Vault-empty grounding** — degrade to source-only rewrite with an explicit "no vault grounding" banner only when every requested retrieval source completed successfully and the authorized vault was genuinely empty. An outage is `retrieval_unavailable`; partial retrieval is labeled and blocks factual approval until a complete Research run succeeds. Never hallucinate citations.
6. **Prompt injection via uploads** — manuscripts and style exemplars are untrusted *data*: every new input class goes through the existing SECURITY BOUNDARY + XML-escaping path; a `challenge`-role source is still never a source of instructions.
7. **Cross-vault leakage** — jobs, cached research packets, and exports must stay bound to the draft's vault; snapshot which vault + evidence a run used.
8. **Privacy / provider policy** — project attachments and vault passages flow to configured LLM endpoints. Draft Room therefore ships disabled by default and requires an administrator to allowlist exact ordinary/sensitive model origins before generation; the UI also discloses the selected provider/model class.
9. **Export leakage** — a derivative draft embeds vault knowledge; exports should be permission-aware and audit-logged.
10. **Editor gap** — no diff/editor component exists today (verified); the center-pane diff view is genuinely new UI work; pick a library deliberately.
11. **Edit preservation** — rewriting must not silently drop qualifications, conflicting evidence, attribution, or inconvenient facts; the standards desk checks for exactly this, and the fact desk's "Stale"/"Contradicted" classes catch source-vs-vault drift.
12. **Prompt drift across roles** — centralize role prompts (ideally in `prompt_versions`) instead of scattered constants.
13. **Nav/mobile duplication** — `MobileBottomNav` maintains a separate item list; easy to miss.

## 8. Phased plan (each phase shippable)

- **Phase 0 — Define quality before building.** Assemble a small gold corpus: single-manuscript rewrite; conflicting references; stale vs current sources; exact quotations; OCR-degraded input; opinion mixed with fact; embedded malicious instructions; sensitive/cross-vault docs; 2–3 approved voices. Validate the locked source, retention, provider, and human-Ready policies against it. This corpus later drives the eval harness.
- **Phase 1 — Honest vertical slice.** Tables + migration; disabled-by-default feature/provider-origin policy; project CRUD + brief (incl. source roles); dedicated project-private input storage and parse-only uploads; research packet + outline artifacts; rewrite with version history; original/rewrite comparison (simplest viable diff); LINT; manual edits; `.md` export; durable jobs + progress. Ships labeled **"Draft generated — not fact-checked"** and cannot be marked Ready. *~1.5–2k backend LOC, ~1–1.5k frontend.*
- **Phase 2 — Editorial quality gates.** Atomic-claim ledger (`draft_claims`/`draft_claim_sources`/`draft_findings`); quote verification; contradiction + freshness findings; copy desk + standards desk with semantic-change loop-back; accept/reject findings; tiers; full audit trail (prompt/model/source/version per run); SSE event stream; `[D#]` labels; human-only Ready.
- **Phase 3 — Product hardening.** Voice profiles/exemplars; `.docx`/PDF export; per-role model routing; richer cost reporting + stage caching (hard call/time budgets already ship with the durable worker); retention controls; promote-to-vault/wiki; slop-metric eval dashboard against the gold corpus; collaboration/named approvers.

**Success measures** (release-gated by blind human review on the gold corpus): % atomic claims supported; exact-quote fidelity (target 100%); citation-to-passage correctness; preservation of required facts/qualifications; human major-edit rate; upload-to-approved time; zero cross-vault leakage; injection-test pass rate; cancel/retry/restart correctness.

## 9. Verification note

Plugin claims: verified first-hand (all agent prompts, guardrails, pre-check tool, state machine, config read at commit `690fcea`; evidence/knowledge write-path wiring checked by grep in revision 2). ragappv3 claims: mapped by three parallel explorer passes, then re-verified by an independent reviewer pass that opened every cited file — all 22 load-bearing claims confirmed. Precision notes from that pass: `files.parsed_text` persistence is at `document_processor.py:2988–2991`; `wiki_compile_jobs` lacks the `input_json`/`retry_count` columns that `kms_compile_jobs` has (hence §4.2 clones the **kms** table shape). Revision-2 additions verified directly: no diff/editor library in the frontend; no callers of the plugin's `saveEvidence`/`addKnowledge`. External detector claims are linked to the primary OpenAI and NIST pages in §4.5; they motivate framing only and no design element depends on their exact numbers. Anchors use `~` where line numbers may drift.

## 10. Decisions locked for the implementation spec

The former blocking questions are resolved for the first implementation:

1. Vault `read` permits an owner-private draft; vault `write` is required for promote/share/publish.
2. MVP export is Markdown. DOCX/PDF are later phases.
3. Persist `tier` in Phase 1, but defer tier-specific automated policy until the claim ledger is present.
4. Phase 1 editing uses the existing Markdown textarea/preview primitives and a small read-only `diff` (jsdiff) view; do not add Monaco or CodeMirror.
5. Inputs have no surprise TTL in Phase 1. Explicit draft deletion removes raw and parsed inputs; archive retains them. Configurable retention is later work.
6. Chat lint is out of scope. The implementation must not couple Draft Room quality policy to chat behavior.
7. Phase 0 starts with synthetic, non-confidential repository fixtures and a versioned rubric. Product owners provide any real house-style exemplars outside the repository.

The normative schema, APIs, stage contracts, tests, acceptance criteria, and ordered delivery plan are in [`SPEC.md`](./SPEC.md).
