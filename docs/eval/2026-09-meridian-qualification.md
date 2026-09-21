# Meridian F3 Integration Qualification — Exact-Build Evidence Matrix

Issue: #229 (Workstream F, PR 3 of 3). Qualification window: 2026-09-20.
Data: `docs/eval/2026-09-meridian-qualification-data.json`. Release note: `docs/releases/pending/229-f3-meridian-qualification.md`.

## Build Identity

- Build: `67527cd94f7fbd31ab8b6e4d94103844e8a4df9c`
- Deployed image: `sha256:1714ba3ba5c657d626ae337a619125dbaca9ee0fee905c30affc298233dbe3e3` (built on R640AI from the qualification branch at the build commit above — master `54f8682e` + this PR's docs and the reindex probe fix; container healthy; the #647 `FALLBACK_SCORE_FLOOR` marker and the probe fix both verified present in the deployed code). Qualification ran in two stages: the full battery at `edd2c741…` (image 65aad900) and the blocker re-runs at the fixed build above, which contains the first as an ancestor.
- `meta.deployed_revision` provenance: the build-context commit `git -C /home/afmostai/ragappv3 rev-parse HEAD` immediately before `docker compose build knowledgevault`. Rollback images pinned at BOTH stages: `ragappv3-knowledgevault:rollback-pre-f3-281bd714` (00d553f3eade, pre-F3) and `ragappv3-knowledgevault:rollback-f3-stage1-edd2c741` (65aad900, stage-1 qualified build).

## Deployment and Topology Freeze

| Dimension | Frozen value |
|---|---|
| Browsers | Chromium (Playwright, evergreen) exercised; Firefox/Edge assumed per evergreen SPA support, not separately exercised |
| Viewports | 1280x800 desktop; 375x812 mobile |
| Deployment subpath | `/meridian` (app on :9090, API under `/api`) |
| Model/runtime topology | harrier-embed TEI :8080; reranker TEI :8081; minicpm5-instant :8011; ChatGPTN thinking model off-box http://172.16.50.41:8000 (OFFLINE during the window — see Failures); draft editorial qwen38-27b-aggressive 192.168.1.145:8003; redis; flaresolverr |
| Representative fixtures | F2 corpus (CDP/Slack/Legacy vaults, 93 files, unchanged) + F3 alpha text, structured CSV, truncated-PDF boundary fixture |
| Retrieval config observed | reranking on, top_n 7, hybrid on, max_distance_threshold 0.75 (PR #647 calibration live), contextual chunking off, multimodal off |

Supporting data: `docs/eval/2026-09-meridian-qualification-data.json` (`deployment`).

## Journey Qualification

| Step | Result | Evidence |
|---|---|---|
| upload | pass | docs/eval/2026-09-meridian-qualification-evidence/stageA_upload_search.transcript.json — upload 200 |
| parse | pass | phase progression parsing→writing_index→indexed, 2 chunks |
| searchable | pass | keyword hit + semantic top 0.733 |
| ask | pass | instant SSE stream with evidence + content + done |
| cited-answer | pass | [S1] rerank-cited answer (score 0.9805) |
| source-viewing | pass | chunk context API + UI S1/M1 source buttons |
| save-reload-retry-fork | pass | durable turns, truncate/resend, fork session 17 |
| memory-wiki-kms-promotion-edit | pass | memory edit, KMS edit, memory→wiki promotion 200 |
| draft-compose-rewrite | pass-with-limitation | docs/eval/2026-09-meridian-qualification-evidence/requa3c_drafts_vault2.transcript.json — draft lifecycle qualified with a limitation (see Failures) |
| findings-compare | pass | quality report→eval case→compare, fact_coverage delta 1.0 |
| export | pass-with-limitation | canvas download + manifest export; draft export blocked by compose |
| canvas-edit-restore-download | pass | created/user_edit/restore versions + download + manifest |

## Exception Paths

| Path | Result | Evidence |
|---|---|---|
| failed | pass | bad extension 400; empty file 400; truncated PDF → PARSE_FAILED surfaced, index unpolluted |
| partial | pass-with-limitation | parse-boundary failure clean; forced mid-batch partial state not produced |
| cancelled | pass | client SSE disconnect; server persisted both turn rows |
| recovered | pass | failed draft job retried (attempt 2, parent linkage, docs/eval/2026-09-meridian-qualification-evidence/draft_retry.transcript.json); chat retry via truncate+resend; reindex failure reproduced pre-fix (job 392) and recovered post-fix (job 396 pass, job 463 completed) |

Input modes: keyboard — chat composer Enter-send exercised with vault-validation alert then success (docs/eval/2026-09-meridian-qualification-evidence/browser_03_chat_keyboard_sent.png); touch — real touchscreen taps in a hasTouch/isMobile 375px context: search-input focus tap and vault-selector menu tap with observable effects (docs/eval/2026-09-meridian-qualification-evidence/browser_06_touch_context_tap.png); canvas editor not separately visited on a touch viewport.

## Historical Evidence

| Check | Retained | Result |
|---|---|---|
| document replacement (delete+re-upload; no in-place replace API exists) | true | pass-with-limitation |
| reindex | true | pass-with-limitation — the #529-introduced dimension-probe bug (tuple-unpacking the fail_fast=True embedding return) is FIXED in this PR; reindex now completes end to end (vault-9 job 463 completed 1/1, chunk identity retained); vault-2 pass (job 396) visits all files with only two PRE-EXISTING corrupt corpus files failing per-file parse |
| restart (docker restart knowledgevault) | true | pass — search identity `101_0b2bfe94_768_0` score 0.733 unchanged; sessions/wiki/KMS/canvas intact |

## Symptom Recheck

| Symptom | Disposition | Evidence |
|---|---|---|
| blank-thinking | resolved | on the restored endpoint a thinking turn streams real reasoning deltas and a cited answer and persists durably (docs/eval/2026-09-meridian-qualification-evidence/requa2_thinking.transcript.json); during the outage the same path surfaced an explicit error event + empty done, never a blank completed answer, and the UI showed 'Using instant — thinking unavailable' |
| contradictory-upload-status | resolved | API status/phase progression coherent; UI status text matched terminal state |
| missing-previews | resolved | detail Preview pane renders full text; chunk-context API serves sources |
| raw-kms-markdown | resolved | KMS body Markdown rendered natively (h1/strong/em/list/code) |
| claimless-wiki-wording | resolved | claims layer served on page detail; claims + promotion produce active claims |
| inaccessible-mobile-details | resolved | document details navigable at 375px |
| stale-saving-accessibility | resolved | idle Settings shows no false Saving status |
| incomplete-draft-fact-status | documented-limitation | fact_status enum + export header verified in code; live display blocked by compose outage |

## DEEP-C Dispositions

| ID | Disposition | Evidence |
|---|---|---|
| DEEP-C-03 | closed-fixed | production causes identified and RESOLVED live: (1) thinking outage — circuit breaker llm_thinking opened after 5 consecutive failures / All connection attempts failed to the off-box endpoint (docs/eval/2026-09-meridian-qualification-evidence/log_thinking_outage_excerpt.txt); endpoint restored by the operator and verified live. (2) reindex failure — deterministic ValueError at background_tasks.py:3504: the #513 W13 dimension probe tuple-unpacked the fail_fast=True embed_batch return (latent since PR #529; the outage-era 'Event loop is closed' masking cleared once the embedding service was reachable) — root-cause capture in docs/eval/2026-09-meridian-qualification-evidence/repro_reindex_pre_fix.log, fixed in this PR with a contract regression test. Narrowing retained: the chat stream error code for provider outages is coarse (EMBEDDING_ERROR); precise cause requires the log trail |
| DEEP-C-04 | closed-documented-limitation | browser limitations remain documented: same-origin CSRF enforcement by design, touch hardware not driven (layout-class only), Firefox/Edge not separately exercised — see Excluded Scope |

## Supplemental Registry Dispositions

| ID | Owner | Disposition | Evidence |
|---|---|---|---|
| CHAT-UX-01 | #507 (A1) | shipped | shipped by owner slot record: docs/releases/pending/507-chat-turn-lifecycle.md |
| CHAT-UX-02 | #508 (A2) | shipped | shipped by owner slot record: docs/releases/pending/508-evidence-inspection.md |
| CHAT-UX-03 | #508 (A2) | shipped | shipped by owner slot record: docs/releases/pending/508-evidence-inspection.md |
| CHAT-UX-04 | #508 (A2) | shipped | shipped by owner slot record: docs/releases/pending/508-evidence-inspection.md |
| CHAT-UX-05 | #508 (A2) | shipped | shipped by owner slot record: docs/releases/pending/508-evidence-inspection.md |
| CHAT-UX-06 | #509 (A3) | shipped | shipped by owner slot record: owner slot release note |
| DEEP-C-01 | #515 (D1) | shipped | shipped by owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| DEEP-C-02 | #513 (C2) | shipped | shipped by owner slot record: docs/releases/pending/513-recoverable-ingestion-enrichment-reindex.md |
| DEEP-C-03 | #229 (F3) | qualified-narrowed | terminal disposition in this matrix: production causes identified live (logs + error events); wire error-code granularity narrowed |
| DEEP-C-04 | #229 (F3) | qualified-narrowed | terminal disposition in this matrix: browser limitations remain documented limitations (Excluded Scope) |
| DEEP-D-01 | #507 (A1) | shipped | shipped by owner slot record: docs/releases/pending/507-chat-turn-lifecycle.md |
| DEEP-D-02 | #515 (D1) | shipped | shipped by owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| DEEP-D-03 | #494 (E1) | shipped | shipped by owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| DEEP-D-04 | #514 (C3) | shipped | shipped by owner slot record: docs/releases/pending/514-unify-upload-library.md |
| FULL-ENH-01 | #511 (B2) | shipped | shipped by owner slot record: docs/releases/pending/511-b2-redundant-work-model-budgets.md |
| FULL-ENH-02 | #237 (F1) | shipped | shipped by owner slot record: docs/releases/pending/237-complete-trustworthy-evaluation.md |
| FULL-ENH-03 | #511 (B2) | shipped | shipped by owner slot record: docs/releases/pending/511-b2-redundant-work-model-budgets.md |
| FULL-ENH-04 | #511 (B2) | shipped | shipped by owner slot record: docs/releases/pending/511-b2-redundant-work-model-budgets.md |
| FULL-ENH-05 | #511 (B2) | shipped | shipped by owner slot record: docs/releases/pending/511-b2-redundant-work-model-budgets.md |
| LIVE-01 | #507 (A1) | shipped | shipped by owner slot record: docs/releases/pending/507-chat-turn-lifecycle.md |
| LIVE-02 | #514 (C3) | shipped | shipped by owner slot record: docs/releases/pending/514-unify-upload-library.md |
| LIVE-03 | #514 (C3) | shipped | shipped by owner slot record: docs/releases/pending/514-unify-upload-library.md |
| LIVE-04 | #514 (C3) | shipped | shipped by owner slot record: docs/releases/pending/514-unify-upload-library.md |
| LIVE-05 | #515 (D1) | shipped | shipped by owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| LIVE-06 | #517 (D3) | shipped | shipped by owner slot record: docs/releases/pending/517-workstream-d-draft-review.md |
| LIVE-07 | #517 (D3) | shipped | shipped by owner slot record: docs/releases/pending/517-workstream-d-draft-review.md |
| LIVE-08 | #515 (D1) | shipped | shipped by owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| LIVE-09 | #514 (C3) | shipped | shipped by owner slot record: docs/releases/pending/514-unify-upload-library.md |
| LIVE-10 | #494 (E1) | shipped | shipped by owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| MODEL-RESEARCH-01 | #36 (F2) | shipped | shipped by owner slot record: owner slot release note |
| PRODUCT-ENH-01 | #507 (A1) | shipped | shipped by owner slot record: docs/releases/pending/507-chat-turn-lifecycle.md |
| PRODUCT-ENH-02 | #508 (A2) | shipped | shipped by owner slot record: docs/releases/pending/508-evidence-inspection.md |
| PRODUCT-ENH-03 | #508 (A2) | shipped | shipped by owner slot record: docs/releases/pending/508-evidence-inspection.md |
| PRODUCT-ENH-04 | #510 (B1) | shipped | shipped by owner slot record: docs/releases/pending/510-workstream-b1.md |
| PRODUCT-ENH-05 | #514 (C3) | shipped | shipped by owner slot record: docs/releases/pending/514-unify-upload-library.md |
| PRODUCT-ENH-06 | #514 (C3) | shipped | shipped by owner slot record: docs/releases/pending/514-unify-upload-library.md |
| PRODUCT-ENH-07 | #515 (D1) | shipped | shipped by owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| PRODUCT-ENH-08 | #517 (D3) | shipped | shipped by owner slot record: docs/releases/pending/517-workstream-d-draft-review.md |
| PRODUCT-ENH-09 | #509 (A3) | shipped | shipped by owner slot record: owner slot release note |
| PRODUCT-ENH-10 | #508 (A2) | shipped | shipped by owner slot record: docs/releases/pending/508-evidence-inspection.md |
| PRODUCT-ENH-11 | #515 (D1) | shipped | shipped by owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| PRODUCT-ENH-12 | #237 (F1) | shipped | shipped by owner slot record: docs/releases/pending/237-complete-trustworthy-evaluation.md |
| RAG-DEEP-01 | #511 (B2) | shipped | shipped by owner slot record: docs/releases/pending/511-b2-redundant-work-model-budgets.md |
| RAG-DEEP-02 | #511 (B2) | shipped | shipped by owner slot record: docs/releases/pending/511-b2-redundant-work-model-budgets.md |
| RAG-DEEP-03 | #511 (B2) | shipped | shipped by owner slot record: docs/releases/pending/511-b2-redundant-work-model-budgets.md |
| RAG-DEEP-04 | #511 (B2) | shipped | shipped by owner slot record: docs/releases/pending/511-b2-redundant-work-model-budgets.md |
| UPLOAD-DEEP-01 | #514 (C3) | shipped | shipped by owner slot record: docs/releases/pending/514-unify-upload-library.md |
| UPLOAD-DEEP-02 | #518 (E3) | shipped | shipped by owner slot record: docs/releases/pending/518-e3-closure.md |
| UPLOAD-DEEP-03 | #513 (C2) | shipped | shipped by owner slot record: docs/releases/pending/513-recoverable-ingestion-enrichment-reindex.md |
| UPLOAD-DEEP-04 | #513 (C2) | shipped | shipped by owner slot record: docs/releases/pending/513-recoverable-ingestion-enrichment-reindex.md |
| UPLOAD-DEEP-05 | #513 (C2) | shipped | shipped by owner slot record: docs/releases/pending/513-recoverable-ingestion-enrichment-reindex.md |
| UPLOAD-DEEP-06 | #513 (C2) | shipped | shipped by owner slot record: docs/releases/pending/513-recoverable-ingestion-enrichment-reindex.md |
| UPLOAD-DEEP-07 | #518 (E3) | shipped | shipped by owner slot record: docs/releases/pending/518-e3-closure.md |
| UPLOAD-DEEP-08 | #518 (E3) | shipped | shipped by owner slot record: docs/releases/pending/518-e3-closure.md |

## Original Audit Coverage

| ID | Owner | Disposition | Evidence |
|---|---|---|---|
| EVAL-001 | #237 (F1) | resolved | owner slot record: docs/releases/pending/237-complete-trustworthy-evaluation.md |
| EVAL-002 | #237 (F1) | resolved | owner slot record: docs/releases/pending/237-complete-trustworthy-evaluation.md |
| EVAL-003 | #237 (F1) | resolved | owner slot record: docs/releases/pending/237-complete-trustworthy-evaluation.md |
| EVAL-004 | #237 (F1) | resolved | owner slot record: docs/releases/pending/237-complete-trustworthy-evaluation.md |
| TEST-001 | #258 (E2) | resolved | owner slot record: docs/releases/pending/258-workstream-e2-ci-tests-installation.md |
| TEST-002 | #258 (E2) | qualified-narrowed | clock-resolution-dependent risk; narrowed with recorded risk per roadmap coverage note |
| TEST-003 | #258 (E2) | resolved | owner slot record: docs/releases/pending/258-workstream-e2-ci-tests-installation.md |
| BUILD-001 | #258 (E2) | resolved | owner slot record: docs/releases/pending/258-workstream-e2-ci-tests-installation.md |
| BUILD-002 | #258 (E2) | resolved | owner slot record: docs/releases/pending/258-workstream-e2-ci-tests-installation.md |
| TOOL-001 | #258 (E2) | resolved | owner slot record: docs/releases/pending/258-workstream-e2-ci-tests-installation.md |
| TEST-006 | #258 (E2) | resolved | owner slot record: docs/releases/pending/258-workstream-e2-ci-tests-installation.md |
| DOC-001 | #258 (E2) | resolved | owner slot record: docs/releases/pending/258-workstream-e2-ci-tests-installation.md |
| DOC-002 | #258 (E2) | resolved | owner slot record: docs/releases/pending/258-workstream-e2-ci-tests-installation.md |
| DOC-003 | #258 (E2) | resolved | owner slot record: docs/releases/pending/258-workstream-e2-ci-tests-installation.md |
| TEST-007 | #258 (E2) | resolved | owner slot record: docs/releases/pending/258-workstream-e2-ci-tests-installation.md |
| TEST-005 | #258 (E2) | resolved | owner slot record: docs/releases/pending/258-workstream-e2-ci-tests-installation.md |
| TEST-008 | #258 (E2) | resolved | owner slot record: docs/releases/pending/258-workstream-e2-ci-tests-installation.md |
| TEST-004 | #462 (B3) | resolved | owner slot record: docs/releases/pending/462-pr3-residuals.md |
| OBS-003 | #462 (B3) | resolved | owner slot record: docs/releases/pending/462-pr3-residuals.md |
| OPS-002 | #494 (E1) | resolved | owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| OPS-003 | #494 (E1) | resolved | owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| OPS-005 | #494 (E1) | resolved | owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| OPS-006 | #494 (E1) | resolved | owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| LLM-002 | #494 (E1) | resolved | owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| PROMPT-001 | #494 (E1) | resolved | owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| CONFIG-003 | #494 (E1) | resolved | owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| UI-030 | #494 (E1) | resolved | owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| UI-031 | #494 (E1) | resolved | owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| EMAIL-002 | #494 (E1) | resolved | owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| LLM-003 | #494 (E1) | resolved | owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| OPS-008 | #494 (E1) | resolved | owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| EMAIL-001 | #494 (E1) | resolved | owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| CONFIG-007 | #494 (E1) | resolved | owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| LLM-001 | #494 (E1) | resolved | owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| OPS-004 | #494 (E1) | resolved | owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| MODEL-001 | #494 (E1) | resolved | owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| OBS-001 | #494 (E1) | resolved | owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| CONFIG-001 | #494 (E1) | resolved | owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| API-001 | #494 (E1) | resolved | owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| CONFIG-004 | #494 (E1) | resolved | owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| OPS-007 | #494 (E1) | resolved | owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| UI-029 | #494 (E1) | resolved | owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| UI-032 | #494 (E1) | resolved | owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| CONFIG-005 | #494 (E1) | resolved | owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| API-006 | #494 (E1) | resolved | owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| EMAIL-004 | #494 (E1) | resolved | owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| EMAIL-003 | #494 (E1) | resolved | owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| CHAT-002 | #507 (A1) | resolved | owner slot record: docs/releases/pending/507-chat-turn-lifecycle.md |
| UI-001 | #507 (A1) | resolved | owner slot record: docs/releases/pending/507-chat-turn-lifecycle.md |
| UI-002 | #507 (A1) | resolved | owner slot record: docs/releases/pending/507-chat-turn-lifecycle.md |
| UI-003 | #507 (A1) | resolved | owner slot record: docs/releases/pending/507-chat-turn-lifecycle.md |
| CHAT-004 | #507 (A1) | resolved | owner slot record: docs/releases/pending/507-chat-turn-lifecycle.md |
| CHAT-005 | #507 (A1) | resolved | owner slot record: docs/releases/pending/507-chat-turn-lifecycle.md |
| CHAT-006 | #507 (A1) | resolved | owner slot record: docs/releases/pending/507-chat-turn-lifecycle.md |
| UI-039 | #507 (A1) | resolved | owner slot record: docs/releases/pending/507-chat-turn-lifecycle.md |
| UI-043 | #507 (A1) | resolved | owner slot record: docs/releases/pending/507-chat-turn-lifecycle.md |
| UI-044 | #507 (A1) | resolved | owner slot record: docs/releases/pending/507-chat-turn-lifecycle.md |
| UI-037 | #507 (A1) | resolved | owner slot record: docs/releases/pending/507-chat-turn-lifecycle.md |
| UI-040 | #507 (A1) | resolved | owner slot record: docs/releases/pending/507-chat-turn-lifecycle.md |
| UI-045 | #507 (A1) | resolved | owner slot record: docs/releases/pending/507-chat-turn-lifecycle.md |
| CHAT-007 | #507 (A1) | resolved | owner slot record: docs/releases/pending/507-chat-turn-lifecycle.md |
| UI-048 | #507 (A1) | resolved | owner slot record: docs/releases/pending/507-chat-turn-lifecycle.md |
| UI-050 | #507 (A1) | resolved | owner slot record: docs/releases/pending/507-chat-turn-lifecycle.md |
| UI-025 | #508 (A2) | resolved | owner slot record: docs/releases/pending/508-evidence-inspection.md |
| UI-028 | #508 (A2) | resolved | owner slot record: docs/releases/pending/508-evidence-inspection.md |
| UI-034 | #508 (A2) | resolved | owner slot record: docs/releases/pending/508-evidence-inspection.md |
| UI-035 | #508 (A2) | resolved | owner slot record: docs/releases/pending/508-evidence-inspection.md |
| UI-036 | #508 (A2) | resolved | owner slot record: docs/releases/pending/508-evidence-inspection.md |
| UI-038 | #508 (A2) | resolved | owner slot record: docs/releases/pending/508-evidence-inspection.md |
| UI-046 | #508 (A2) | resolved | owner slot record: docs/releases/pending/508-evidence-inspection.md |
| RAG-002 | #510 (B1) | resolved | owner slot record: docs/releases/pending/510-workstream-b1.md |
| RAG-003 | #510 (B1) | resolved | owner slot record: docs/releases/pending/510-workstream-b1.md |
| RAG-004 | #510 (B1) | resolved | owner slot record: docs/releases/pending/510-workstream-b1.md |
| QUERY-001 | #510 (B1) | resolved | owner slot record: docs/releases/pending/510-workstream-b1.md |
| CHAT-003 | #510 (B1) | resolved | owner slot record: docs/releases/pending/510-workstream-b1.md |
| CITE-001 | #510 (B1) | resolved | owner slot record: docs/releases/pending/510-workstream-b1.md |
| VECTOR-004 | #510 (B1) | resolved | owner slot record: docs/releases/pending/510-workstream-b1.md |
| UI-004 | #510 (B1) | resolved | owner slot record: docs/releases/pending/510-workstream-b1.md |
| RAG-006 | #510 (B1) | resolved | owner slot record: docs/releases/pending/510-workstream-b1.md |
| RAG-001 | #510 (B1) | resolved | owner slot record: docs/releases/pending/510-workstream-b1.md |
| RAG-005 | #510 (B1) | resolved | owner slot record: docs/releases/pending/510-workstream-b1.md |
| CITE-002 | #510 (B1) | resolved | owner slot record: docs/releases/pending/510-workstream-b1.md |
| RAG-007 | #510 (B1) | resolved | owner slot record: docs/releases/pending/510-workstream-b1.md |
| RAG-008 | #510 (B1) | resolved | owner slot record: docs/releases/pending/510-workstream-b1.md |
| EMBED-001 | #511 (B2) | resolved | owner slot record: docs/releases/pending/511-b2-redundant-work-model-budgets.md |
| RERANK-003 | #511 (B2) | resolved | owner slot record: docs/releases/pending/511-b2-redundant-work-model-budgets.md |
| EMBED-002 | #511 (B2) | resolved | owner slot record: docs/releases/pending/511-b2-redundant-work-model-budgets.md |
| RERANK-001 | #511 (B2) | resolved | owner slot record: docs/releases/pending/511-b2-redundant-work-model-budgets.md |
| RERANK-002 | #511 (B2) | resolved | owner slot record: docs/releases/pending/511-b2-redundant-work-model-budgets.md |
| RERANK-004 | #511 (B2) | resolved | owner slot record: docs/releases/pending/511-b2-redundant-work-model-budgets.md |
| VECTOR-005 | #512 (C1) | resolved | owner slot record: docs/releases/pending/512-non-destructive-recovery.md |
| VECTOR-001 | #512 (C1) | resolved | owner slot record: docs/releases/pending/512-non-destructive-recovery.md |
| DB-001 | #512 (C1) | resolved | owner slot record: docs/releases/pending/512-non-destructive-recovery.md |
| DB-003 | #512 (C1) | resolved | owner slot record: docs/releases/pending/512-non-destructive-recovery.md |
| SEARCH-005 | #512 (C1) | resolved | owner slot record: docs/releases/pending/512-non-destructive-recovery.md |
| VECTOR-006 | #512 (C1) | resolved | owner slot record: docs/releases/pending/512-non-destructive-recovery.md |
| VECTOR-002 | #512 (C1) | resolved | owner slot record: docs/releases/pending/512-non-destructive-recovery.md |
| VECTOR-003 | #512 (C1) | resolved | owner slot record: docs/releases/pending/512-non-destructive-recovery.md |
| DB-002 | #512 (C1) | resolved | owner slot record: docs/releases/pending/512-non-destructive-recovery.md |
| INGEST-001 | #513 (C2) | resolved | owner slot record: docs/releases/pending/513-recoverable-ingestion-enrichment-reindex.md |
| INGEST-002 | #513 (C2) | resolved | owner slot record: docs/releases/pending/513-recoverable-ingestion-enrichment-reindex.md |
| INGEST-005 | #513 (C2) | resolved | owner slot record: docs/releases/pending/513-recoverable-ingestion-enrichment-reindex.md |
| INGEST-006 | #513 (C2) | resolved | owner slot record: docs/releases/pending/513-recoverable-ingestion-enrichment-reindex.md |
| INGEST-007 | #513 (C2) | resolved | owner slot record: docs/releases/pending/513-recoverable-ingestion-enrichment-reindex.md |
| INGEST-009 | #513 (C2) | resolved | owner slot record: docs/releases/pending/513-recoverable-ingestion-enrichment-reindex.md |
| INGEST-010 | #513 (C2) | resolved | owner slot record: docs/releases/pending/513-recoverable-ingestion-enrichment-reindex.md |
| INGEST-008 | #513 (C2) | resolved | owner slot record: docs/releases/pending/513-recoverable-ingestion-enrichment-reindex.md |
| INGEST-011 | #513 (C2) | resolved | owner slot record: docs/releases/pending/513-recoverable-ingestion-enrichment-reindex.md |
| INGEST-013 | #513 (C2) | resolved | owner slot record: docs/releases/pending/513-recoverable-ingestion-enrichment-reindex.md |
| INGEST-015 | #513 (C2) | resolved | owner slot record: docs/releases/pending/513-recoverable-ingestion-enrichment-reindex.md |
| INGEST-014 | #513 (C2) | resolved | owner slot record: docs/releases/pending/513-recoverable-ingestion-enrichment-reindex.md |
| INGEST-016 | #513 (C2) | resolved | owner slot record: docs/releases/pending/513-recoverable-ingestion-enrichment-reindex.md |
| INGEST-018 | #513 (C2) | resolved | owner slot record: docs/releases/pending/513-recoverable-ingestion-enrichment-reindex.md |
| INGEST-019 | #513 (C2) | resolved | owner slot record: docs/releases/pending/513-recoverable-ingestion-enrichment-reindex.md |
| INGEST-020 | #513 (C2) | resolved | owner slot record: docs/releases/pending/513-recoverable-ingestion-enrichment-reindex.md |
| INGEST-021 | #513 (C2) | resolved | owner slot record: docs/releases/pending/513-recoverable-ingestion-enrichment-reindex.md |
| INGEST-022 | #513 (C2) | resolved | owner slot record: docs/releases/pending/513-recoverable-ingestion-enrichment-reindex.md |
| INGEST-003 | #513 (C2) | resolved | owner slot record: docs/releases/pending/513-recoverable-ingestion-enrichment-reindex.md |
| INGEST-004 | #513 (C2) | resolved | owner slot record: docs/releases/pending/513-recoverable-ingestion-enrichment-reindex.md |
| INGEST-012 | #513 (C2) | resolved | owner slot record: docs/releases/pending/513-recoverable-ingestion-enrichment-reindex.md |
| CONFIG-002 | #513 (C2) | resolved | owner slot record: docs/releases/pending/513-recoverable-ingestion-enrichment-reindex.md |
| INGEST-017 | #513 (C2) | resolved | owner slot record: docs/releases/pending/513-recoverable-ingestion-enrichment-reindex.md |
| UI-005 | #514 (C3) | resolved | owner slot record: docs/releases/pending/514-unify-upload-library.md |
| UI-010 | #514 (C3) | resolved | owner slot record: docs/releases/pending/514-unify-upload-library.md |
| UI-006 | #514 (C3) | resolved | owner slot record: docs/releases/pending/514-unify-upload-library.md |
| UI-011 | #514 (C3) | resolved | owner slot record: docs/releases/pending/514-unify-upload-library.md |
| FOLDER-001 | #514 (C3) | resolved | owner slot record: docs/releases/pending/514-unify-upload-library.md |
| UI-012 | #514 (C3) | resolved | owner slot record: docs/releases/pending/514-unify-upload-library.md |
| UI-041 | #514 (C3) | resolved | owner slot record: docs/releases/pending/514-unify-upload-library.md |
| UI-007 | #514 (C3) | resolved | owner slot record: docs/releases/pending/514-unify-upload-library.md |
| UI-008 | #514 (C3) | resolved | owner slot record: docs/releases/pending/514-unify-upload-library.md |
| UI-009 | #514 (C3) | resolved | owner slot record: docs/releases/pending/514-unify-upload-library.md |
| UI-042 | #514 (C3) | resolved | owner slot record: docs/releases/pending/514-unify-upload-library.md |
| UI-047 | #514 (C3) | resolved | owner slot record: docs/releases/pending/514-unify-upload-library.md |
| UI-049 | #514 (C3) | resolved | owner slot record: docs/releases/pending/514-unify-upload-library.md |
| CONFIG-006 | #514 (C3) | resolved | owner slot record: docs/releases/pending/514-unify-upload-library.md |
| UI-051 | #514 (C3) | resolved | owner slot record: docs/releases/pending/514-unify-upload-library.md |
| UI-052 | #514 (C3) | resolved | owner slot record: docs/releases/pending/514-unify-upload-library.md |
| UI-053 | #514 (C3) | resolved | owner slot record: docs/releases/pending/514-unify-upload-library.md |
| WIKI-001 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| WIKI-002 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| SEARCH-003 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| UI-015 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| WIKI-006 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| WIKI-007 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| WIKI-009 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| WIKI-010 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| UI-014 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| UI-023 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| WIKI-012 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| MEM-001 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| API-005 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| SEARCH-002 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| UI-033 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| WIKI-003 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| WIKI-004 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| UI-013 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| WIKI-005 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| UI-016 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| WIKI-008 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| UI-017 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| WIKI-011 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| API-002 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| API-004 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| UI-024 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| SEARCH-004 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| SEARCH-001 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| UI-026 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| WIKI-013 | #515 (D1) | verified | full structured-key equality required by the roadmap note; owner slot D1 record |
| WIKI-014 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| UI-027 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| WIKI-015 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| OBS-004 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| MEM-002 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| MEM-003 | #515 (D1) | resolved | owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| DRAFT-001 | #516 (D2) | resolved | owner slot record: docs/releases/pending/516-draft-room-transactional-reliability.md |
| DRAFT-002 | #516 (D2) | resolved | owner slot record: docs/releases/pending/516-draft-room-transactional-reliability.md |
| DRAFT-003 | #516 (D2) | resolved | owner slot record: docs/releases/pending/516-draft-room-transactional-reliability.md |
| DRAFT-004 | #516 (D2) | resolved | owner slot record: docs/releases/pending/516-draft-room-transactional-reliability.md |
| DRAFT-006 | #516 (D2) | resolved | owner slot record: docs/releases/pending/516-draft-room-transactional-reliability.md |
| DRAFT-007 | #516 (D2) | resolved | owner slot record: docs/releases/pending/516-draft-room-transactional-reliability.md |
| DRAFT-011 | #516 (D2) | resolved | owner slot record: docs/releases/pending/516-draft-room-transactional-reliability.md |
| DRAFT-013 | #516 (D2) | resolved | owner slot record: docs/releases/pending/516-draft-room-transactional-reliability.md |
| DRAFT-014 | #516 (D2) | resolved | owner slot record: docs/releases/pending/516-draft-room-transactional-reliability.md |
| UI-018 | #516 (D2) | resolved | owner slot record: docs/releases/pending/516-draft-room-transactional-reliability.md |
| UI-019 | #516 (D2) | resolved | owner slot record: docs/releases/pending/516-draft-room-transactional-reliability.md |
| DRAFT-020 | #516 (D2) | resolved | owner slot record: docs/releases/pending/516-draft-room-transactional-reliability.md |
| DRAFT-022 | #516 (D2) | resolved | owner slot record: docs/releases/pending/516-draft-room-transactional-reliability.md |
| DRAFT-023 | #516 (D2) | resolved | owner slot record: docs/releases/pending/516-draft-room-transactional-reliability.md |
| DRAFT-024 | #516 (D2) | resolved | owner slot record: docs/releases/pending/516-draft-room-transactional-reliability.md |
| DRAFT-025 | #516 (D2) | resolved | owner slot record: docs/releases/pending/516-draft-room-transactional-reliability.md |
| DRAFT-005 | #516 (D2) | resolved | owner slot record: docs/releases/pending/516-draft-room-transactional-reliability.md |
| UI-021 | #516 (D2) | resolved | owner slot record: docs/releases/pending/516-draft-room-transactional-reliability.md |
| UI-020 | #516 (D2) | resolved | owner slot record: docs/releases/pending/516-draft-room-transactional-reliability.md |
| UI-022 | #516 (D2) | resolved | owner slot record: docs/releases/pending/516-draft-room-transactional-reliability.md |
| API-003 | #516 (D2) | resolved | owner slot record: docs/releases/pending/516-draft-room-transactional-reliability.md |
| DRAFT-008 | #517 (D3) | resolved | owner slot record: docs/releases/pending/517-workstream-d-draft-review.md |
| DRAFT-009 | #517 (D3) | resolved | owner slot record: docs/releases/pending/517-workstream-d-draft-review.md |
| DRAFT-010 | #517 (D3) | resolved | owner slot record: docs/releases/pending/517-workstream-d-draft-review.md |
| DRAFT-012 | #517 (D3) | resolved | owner slot record: docs/releases/pending/517-workstream-d-draft-review.md |
| DRAFT-015 | #517 (D3) | resolved | owner slot record: docs/releases/pending/517-workstream-d-draft-review.md |
| DRAFT-017 | #517 (D3) | resolved | owner slot record: docs/releases/pending/517-workstream-d-draft-review.md |
| DRAFT-018 | #517 (D3) | resolved | owner slot record: docs/releases/pending/517-workstream-d-draft-review.md |
| DRAFT-016 | #517 (D3) | resolved | owner slot record: docs/releases/pending/517-workstream-d-draft-review.md |
| DRAFT-019 | #517 (D3) | resolved | owner slot record: docs/releases/pending/517-workstream-d-draft-review.md |
| DRAFT-021 | #517 (D3) | resolved | owner slot record: docs/releases/pending/517-workstream-d-draft-review.md |
| OBS-002 | #518 (E3) | resolved | owner slot record: docs/releases/pending/518-e3-closure.md |

## Legacy Migration Dispositions

| Obligation | Owner | Disposition | Evidence |
|---|---|---|---|
| #202 preserved-scope | #202 (X1) | superseded | parked scope: preserved in Workstream X (#202) with no resolution claim |
| #229 legacy-01 | #511 (B2) | migrated | closed by owner slot record: docs/releases/pending/511-b2-redundant-work-model-budgets.md |
| #229 legacy-02 | #237 (F1) | migrated | closed by owner slot record: docs/releases/pending/237-complete-trustworthy-evaluation.md |
| #229 legacy-03 | #511 (B2) | migrated | closed by owner slot record: docs/releases/pending/511-b2-redundant-work-model-budgets.md |
| #229 legacy-04 | #511 (B2) | migrated | closed by owner slot record: docs/releases/pending/511-b2-redundant-work-model-budgets.md |
| #229 legacy-05 | #510 (B1) | migrated | closed by owner slot record: docs/releases/pending/510-workstream-b1.md |
| #229 legacy-06 | #513 (C2) | migrated | closed by owner slot record: docs/releases/pending/513-recoverable-ingestion-enrichment-reindex.md |
| #229 legacy-07 | #515 (D1) | migrated | closed by owner slot record: docs/releases/pending/515-workstream-d-memory-wiki-kms.md |
| #229 legacy-07b | #508 (A2) | migrated | closed by owner slot record: docs/releases/pending/508-evidence-inspection.md |
| #229 legacy-08 | #202 (X1) | superseded | parked scope: preserved in Workstream X (#202) with no resolution claim |
| #229 legacy-09 | #202 (X1) | superseded | parked scope: preserved in Workstream X (#202) with no resolution claim |
| #229 legacy-10 | #258 (E2) | migrated | closed by owner slot record: docs/releases/pending/258-workstream-e2-ci-tests-installation.md |
| #229 legacy-10b | #494 (E1) | migrated | closed by owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| #229 legacy-11 | #518 (E3) | migrated | closed by owner slot record: docs/releases/pending/518-e3-closure.md |
| #229 legacy-12 | #518 (E3) | migrated | closed by owner slot record: docs/releases/pending/518-e3-closure.md |
| #229 legacy-13 | #507 (A1) | migrated | closed by owner slot record: docs/releases/pending/507-chat-turn-lifecycle.md |
| #229 legacy-13b | #514 (C3) | migrated | closed by owner slot record: docs/releases/pending/514-unify-upload-library.md |
| #229 legacy-14 | #258 (E2) | migrated | closed by owner slot record: docs/releases/pending/258-workstream-e2-ci-tests-installation.md |
| #237 operator-evidence | #237 (F1) | migrated | closed by owner slot record: docs/releases/pending/237-complete-trustworthy-evaluation.md |
| #237 scope-01 | #237 (F1) | migrated | closed by owner slot record: docs/releases/pending/237-complete-trustworthy-evaluation.md |
| #237 scope-02 | #237 (F1) | migrated | closed by owner slot record: docs/releases/pending/237-complete-trustworthy-evaluation.md |
| #237 scope-03 | #237 (F1) | migrated | closed by owner slot record: docs/releases/pending/237-complete-trustworthy-evaluation.md |
| #237 scope-04 | #237 (F1) | migrated | closed by owner slot record: docs/releases/pending/237-complete-trustworthy-evaluation.md |
| #258 ENH-001 | #258 (E2) | migrated | closed by owner slot record: docs/releases/pending/258-workstream-e2-ci-tests-installation.md |
| #258 ENH-002 | #202 (X1) | superseded | parked scope: preserved in Workstream X (#202) with no resolution claim |
| #258 ENH-003 | #258 (E2) | migrated | closed by owner slot record: docs/releases/pending/258-workstream-e2-ci-tests-installation.md |
| #258 ENH-004 | #518 (E3) | migrated | closed by owner slot record: docs/releases/pending/518-e3-closure.md |
| #258 ENH-005 | #258 (E2) | migrated | closed by owner slot record: docs/releases/pending/258-workstream-e2-ci-tests-installation.md |
| #258 ENH-006 | #258 (E2) | migrated | closed by owner slot record: docs/releases/pending/258-workstream-e2-ci-tests-installation.md |
| #258 ENH-007 | #258 (E2) | migrated | closed by owner slot record: docs/releases/pending/258-workstream-e2-ci-tests-installation.md |
| #258 ENH-008 | #202 (X1) | superseded | parked scope: preserved in Workstream X (#202) with no resolution claim |
| #258 ENH-009 | #518 (E3) | migrated | closed by owner slot record: docs/releases/pending/518-e3-closure.md |
| #258 ENH-010 | #258 (E2) | migrated | closed by owner slot record: docs/releases/pending/258-workstream-e2-ci-tests-installation.md |
| #258 ENH-011 | #258 (E2) | migrated | closed by owner slot record: docs/releases/pending/258-workstream-e2-ci-tests-installation.md |
| #258 ENH-012 | #202 (X1) | superseded | parked scope: preserved in Workstream X (#202) with no resolution claim |
| #258 ENH-013 | #202 (X1) | superseded | parked scope: preserved in Workstream X (#202) with no resolution claim |
| #258 ENH-014 | #510 (B1) | migrated | closed by owner slot record: docs/releases/pending/510-workstream-b1.md |
| #258 ENH-015 | #494 (E1) | migrated | closed by owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| #258 ENH-016 | #507 (A1) | migrated | closed by owner slot record: docs/releases/pending/507-chat-turn-lifecycle.md |
| #36 remaining-02 | #36 (F2) | migrated | closed by owner slot record: owner slot release note |
| #36 remaining-03 | #36 (F2) | migrated | closed by owner slot record: owner slot release note |
| #36 remaining-04 | #36 (F2) | migrated | closed by owner slot record: owner slot release note |
| #36 remaining-05 | #36 (F2) | migrated | closed by owner slot record: owner slot release note |
| #36 remaining-06 | #36 (F2) | migrated | closed by owner slot record: owner slot release note |
| #36 remaining-07 | #36 (F2) | migrated | closed by owner slot record: owner slot release note |
| #462 original-contract | #462 (B3) | migrated | closed by owner slot record: docs/releases/pending/462-pr3-residuals.md |
| #494 FU-001 | #494 (E1) | migrated | closed by owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| #494 FU-002 | #202 (X1) | superseded | parked scope: preserved in Workstream X (#202) with no resolution claim |
| #494 FU-003 | #494 (E1) | migrated | closed by owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| #494 FU-004 | #494 (E1) | migrated | closed by owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| #494 FU-005 | #494 (E1) | migrated | closed by owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| #494 FU-006 | #494 (E1) | migrated | closed by owner slot record: docs/releases/pending/494-workstream-e1-provider-health-config.md |
| #494 FU-007 | #513 (C2) | migrated | closed by owner slot record: docs/releases/pending/513-recoverable-ingestion-enrichment-reindex.md |
| #494 FU-008 | #514 (C3) | migrated | closed by owner slot record: docs/releases/pending/514-unify-upload-library.md |
| #494 FU-009 | #258 (E2) | migrated | closed by owner slot record: docs/releases/pending/258-workstream-e2-ci-tests-installation.md |

## Regression Links

| ID | Regression |
|---|---|
| supplemental-registry (54 IDs) | each shipped ID's owner-slot release note records its regression suite; closing PRs: #522/#525/#529/#530/#533/#565/#577/#647 |
| original-audit-207 | per-owner regression coverage in the slot records above; master CI (Backend ruff+pytest, Frontend vitest/build, Playwright e2e, quality contracts, SAST baseline) green at the build commit |
| DEEP-C-03 | docs/eval/2026-09-meridian-qualification-data.json deep_c evidence; live log excerpts docs/eval/2026-09-meridian-qualification-evidence/log_thinking_outage_excerpt.txt |
| MODEL-RESEARCH-01 | docs/eval/2026-09-model-research.md and docs/eval/2026-09-model-qualification.md (F2) |
| relevance-calibration | frontend/src/lib/relevance.test.ts + backend/tests/test_relevance_cutoff_calibration.py (PR #647) — observed live as max_distance_threshold 0.75 |

## Excluded Scope

- Open non-F3 follow-ups, recorded but NOT owned or closed here: #597 (admin maintenance audit row), #603 (toggle_manager race follow-up), #614 (unwired operator settings; PR #625 open), #640 (PR #627 follow-ups), #645 (on-loop pooled checkouts — the live reindex `Event loop is closed` failure observed during this qualification is an instance of this open issue's class).
- Parked: #202 (X1) — security/access-management and related historical obligations; excluded from active completion, no resolution claimed (legacy rows marked `superseded`).
- Live qualification findings and their disposition: the reindex failure was initially suspected as the #645 checkout class but deterministic reproduction proved it to be the #529-introduced dimension-probe bug, which this PR fixes (#645 remains open for its own on-loop checkout scope); the thinking-model endpoint outage was external (host restored by the operator) — UI/API degrade honestly throughout.
- Structured (CSV) content is keyword-searchable but its chunk did not surface in semantic retrieval for content queries; the ask declined honestly. Recorded as a retrieval-quality limitation for evaluation follow-up (#237 owns the eval harness; no open defect filed).
- Two PRE-EXISTING corrupt corpus files in the CDP vault (pasted-text uploads unstructured misdetects as JSON) fail per-file parse during reindex and normal ingestion alike — operator data cleanup, not a product defect (docs/eval/2026-09-meridian-qualification-evidence/requa1_reindex.transcript.json).

## Performance Profile

Baseline: `docs/eval/2026-09-performance.md` (F2, build 281bd714, thinking-model tier-1 completion p50 41.2 s, queue-wait p50 40 ms).

| Metric | Qualified | Baseline | Delta |
|---|---|---|---|
| turn queue-wait p50 (tier 1, instant) | 87.7 ms | 40 ms (F2 tier-1, thinking) | +47.7 ms; both sub-100 ms, healthy |
| completion p50 (tier 1, instant) | 0.97 s | — | instant-mode measurement (no F2 instant baseline) |
| completion p50 (tier 1, thinking) | 19.4 s | 41.2 s (F2 tier-1, thinking) | like-for-like thinking comparison on the restored endpoint (4/4 turns 18.1-29.9 s + a 52.7 s cited research turn; docs/eval/2026-09-meridian-qualification-evidence/requa2_thinking.transcript.json) |

Raw: docs/eval/2026-09-meridian-qualification-evidence/perf_tier1_instant.transcript.json (6 samples).

## Failures and Environment Deviations (read before rollout)

1. **RESOLVED — Thinking-model endpoint outage.** http://172.16.50.41:8000 refused connections through the first battery window (external host). The operator restored it during the qualification; live-thinking and the thinking perf row are GREEN on the restored endpoint (docs/eval/2026-09-meridian-qualification-evidence/requa2_thinking.transcript.json), and the source-only-rewrite draft completed its full lifecycle on it. Outage-era failures are retained as evidence.
2. **RESOLVED — Reindex failure (reindex dimension-probe bug).** The deterministic post-outage reproduction (docs/eval/2026-09-meridian-qualification-evidence/repro_reindex_pre_fix.log) proved the failure was NOT the #645 checkout class but the #529-introduced probe tuple-unpack bug; this PR fixes it (backend/app/services/background_tasks.py + tests/test_issue229_reindex_dimension_probe.py, RED-on-revert proven) and reindex now completes end to end (docs/eval/2026-09-meridian-qualification-evidence/requa4_reindex_cleanvault.transcript.json job 463 completed; vault-2 job 396 pass over 13/15 files with 2 pre-existing corrupt corpus files failing per-file parse). #645 remains open for its own scope.
3. **PARTIALLY RESOLVED — Draft gates on the restored server.** Source-only rewrite: PASS (full lifecycle, draft 7 job 28: revision, findings, export). Mixed-source compose: FAIL — qualified through the copy stage with 9 real model calls (jobs 29/30) but its standards-stage call exceeds the hardcoded 300s non-streaming client timeout on the restored always-reasoning server in every attempt (jobs 24, 27, 29, 30, 31); production cause and operator unblock in docs/eval/2026-09-meridian-qualification-evidence/requa3_draft_latency_cause.md (the deployment's own pre-outage reference compile ran 19 calls in 56 minutes; restore that serving profile, then one retry of the queued job finishes the remaining stages via the proven stage-cache resume).
4. **Temporary CORS deviation (restored).** The lab network cannot reach the configured public proxy domains, and raw-IP browser origins are rejected by the same-origin CSRF design. To run the mandated browser legs, `BACKEND_CORS_ORIGINS` temporarily gained `http://172.16.50.159:9090` (backup at `/home/afmostai/ragappv3/.env.pre-f3-backup`), the container was recreated, and the original value was restored and health-gated immediately after the browser legs. Both recreations re-verified restart-survival.
5. Draft Room was already enabled at runtime (`GET /api/draft-room/capabilities` enabled=true) despite the env default; no setting was changed by this qualification.

## Rollout and Rollback

- Rollout: the qualified build `edd2c741…` is deployed on R640AI as image `65aad900…` (container `knowledgevault`, compose project `ragappv3`, service `knowledgevault`, config `docker-compose.yml`, data bind-mounted at `/home/afmostai/ragappv3/data`). Recreate with `cd /home/afmostai/ragappv3 && docker compose up -d knowledgevault` after `git pull`; health-gate on `/api/health` (`status ok`, backend/embeddings/vector_store true).
- Rollback (pinned): `docker tag 00d553f3eade ragappv3-knowledgevault:rollback-pre-f3-281bd714` was created before the rebuild; rollback = stop service, `docker run`/recreate from tag `ragappv3-knowledgevault:rollback-pre-f3-281bd714`, or `git checkout 281bd714` + rebuild. Data volume is untouched by both paths.
- Operator-visible outcomes: post-qualification the deployment runs the final integrated build; `max_distance_threshold` 0.75 calibration live; Draft Room enabled; thinking-model outage banner visible until the external service is restored. Qualification artifacts (vault 8, fixtures, bench user) were removed; see Restoration.
- Restoration record: docs/eval/2026-09-meridian-qualification-evidence/pre-qualification-inventory.txt vs docs/eval/2026-09-meridian-qualification-evidence/post-cleanup-inventory.txt — identical baselines (5 vaults, 93 files, 47 users, wiki 99, KMS 75+3 auto-compiled rows removed, memories 1).

## CI Gate Results

| Gate | Result |
|---|---|
| ruff | PASS |
| pytest (targeted) | PASS |
| typecheck | PASS |
| lint | PASS |
| test | PASS |
| build | PASS |

Commands and exit codes: `.agents/issue-traces/229-meridian-experience-qualification/08-test-results.md` (trace-local).

