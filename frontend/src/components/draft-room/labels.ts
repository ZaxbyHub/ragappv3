/**
 * Normative Draft Room product language.
 *
 * Issue #437 and `specs/draft-room/SPEC.md` fix this copy exactly. Every Draft Room surface
 * must import from here rather than inlining a string, so a wording change stays a one-line
 * edit and so the honesty constraints below cannot drift apart across components.
 *
 * Honesty constraints (SPEC 1.7, 1.8, 11.7, 11.9, 12.3, 16.6, 20):
 * - `Ready` means a human approved the current fact-checked revision under this workflow.
 *   It is never a claim that the text is universally true or published.
 * - The per-citation lexical-overlap number is a word-overlap measure. It must never be
 *   labelled confidence, factual confidence, support probability, verification, or entailment.
 * - Never say "AI detector passed", "human-written", "factually true", "verified true",
 *   or "published".
 */

/** Product and navigation. */
export const DRAFT_ROOM_NAV_LABEL = "Draft Room";
export const DRAFT_ROOM_PAGE_DESCRIPTION =
  "Rewrite and compose against private source files and your selected vault.";

/** Primary calls to action. */
export const CREATE_PROJECT_HEADING = "Create a drafting project";
export const NEW_DRAFT_CTA = "New draft";
export const ADD_SOURCE_FILES_CTA = "Add source files";
export const REWRITE_DRAFT_CTA = "Rewrite draft";
export const COMPOSE_DRAFT_CTA = "Create draft";
export const MARK_READY_CTA = "Mark Ready";
export const PROMOTE_TO_VAULT_CTA = "Promote to vault";
export const SAVE_REVISION_CTA = "Save new revision";
export const EXPORT_CTA = "Export";

/** Compile CTA depends on the project's mode. */
export function compileCtaLabel(mode: "rewrite" | "compose"): string {
  return mode === "rewrite" ? REWRITE_DRAFT_CTA : COMPOSE_DRAFT_CTA;
}

/** Pipeline status language. */
export const NEWSROOM_IN_PROGRESS = "Newsroom in progress";
export const DRAFT_COMPLETE_REVIEW_REQUIRED = "Draft complete — review required";
export const DRAFT_NOT_FACT_CHECKED = "Draft generated — not fact-checked";

/** Blocking and warning banners. */
export const SOURCE_ONLY_WARNING = "No vault evidence was available for this run";
export const RETRIEVAL_PARTIAL_WARNING =
  "Some vault sources were unavailable; factual approval is blocked";
export const EVIDENCE_INVALIDATED_WARNING =
  "Sources changed after fact-checking; run the newsroom again";
export const SOURCE_DELETED_WARNING = "Source deleted after this revision";
export const VAULT_ACCESS_REVOKED_WARNING =
  "You no longer have read access to this project's vault. You can still cancel runs and delete the project.";
export const ARCHIVED_READ_ONLY_WARNING =
  "This project is archived and read-only until restored.";

/** Evidence and claim language. SPEC 16.6 forbids "verified true". */
export const SUPPORTED_BY_EVIDENCE = "Supported by captured evidence";
export const LEXICAL_OVERLAP_LABEL = "Citation lexical overlap";

/**
 * Renders the lexical-overlap score under its only permitted label.
 * SPEC 12.3 forbids calling this confidence, support probability, verification or entailment.
 */
export function lexicalOverlapText(score: number | null | undefined): string | null {
  if (score === null || score === undefined || Number.isNaN(score)) return null;
  return `${LEXICAL_OVERLAP_LABEL}: ${score.toFixed(2)}`;
}

/** What `Ready` does and does not mean, shown at the approval gate. */
export const READY_MEANING =
  "Ready records that you approved this exact fact-checked revision under this workflow. " +
  "It is not a claim that the text is universally true, and it does not publish anything.";

/** Disclosure shown before a compile run sends content to the configured provider. */
export const PROVIDER_DISCLOSURE =
  "Selected project text and matching vault passages are sent to the configured model provider " +
  "for this run. The result always requires human review.";

/** Draft status display names, keyed by the backend `DraftStatus` values. */
export const DRAFT_STATUS_LABELS: Record<string, string> = {
  draft: "Draft",
  queued: "Queued",
  running: NEWSROOM_IN_PROGRESS,
  needs_review: "Needs review",
  ready: "Ready",
  failed: "Failed",
  cancelled: "Cancelled",
  archived: "Archived",
};

/** List-page filter tabs, in display order. `null` means "no status filter". */
export const DRAFT_LIST_FILTERS: ReadonlyArray<{ id: string; label: string; status: string | null }> = [
  { id: "all", label: "All", status: null },
  { id: "draft", label: "Draft", status: "draft" },
  { id: "running", label: "In progress", status: "running" },
  { id: "needs_review", label: "Needs review", status: "needs_review" },
  { id: "ready", label: "Ready", status: "ready" },
  { id: "archived", label: "Archived", status: "archived" },
];

/** Pipeline stage display names, keyed by the backend `StageName` values. */
export const STAGE_LABELS: Record<string, string> = {
  intake: "Assignment",
  research: "Research",
  outline: "Outline",
  draft: "Draft",
  lint: "Lint",
  copy: "Copy",
  standards: "Standards",
  fact: "Fact",
  assemble: "Assemble",
};

/** Claim status display names, keyed by the backend `ClaimStatus` values. */
export const CLAIM_STATUS_LABELS: Record<string, string> = {
  supported: "Supported",
  contradicted: "Contradicted",
  ambiguous: "Ambiguous",
  stale: "Stale",
  unsupported: "Unsupported",
  opinion: "Opinion",
};

/**
 * Finding category display names. Human labels only — the raw category code
 * stays available on demand through each finding row's diagnostics disclosure.
 */
export const FINDING_CATEGORY_LABELS: Record<string, string> = {
  boilerplate: "Boilerplate",
  style: "Style",
  preservation: "Preservation",
  factuality: "Factuality",
  quote: "Quotes",
  citation: "Citations",
  conflict: "Source conflict",
  security: "Security",
  operational: "Operational",
};

/**
 * Sentence-case explanations for finding categories, used when no
 * rule-specific explanation exists. Never embeds the raw rule id or category
 * code — those live in the diagnostics disclosure (issue #517 AC13a).
 */
export const FINDING_CATEGORY_EXPLANATIONS: Record<string, string> = {
  boilerplate: "This finding rewrites stock filler phrasing so the piece makes a specific claim.",
  style: "This finding concerns wording and style, not the factual accuracy of the draft.",
  preservation: "This finding protects text that must stay unchanged, such as quotes, numbers, and uncertainty.",
  factuality: "This finding concerns how a claim in the draft lines up with the evidence captured for this run.",
  quote: "This finding concerns quoted material and how it is attributed.",
  citation: "This finding concerns the citation labels attached to the draft.",
  conflict: "This finding marks a conflict between the draft and the captured sources.",
  security: "This finding marks content that must not leave the vault.",
  operational: "This finding reports how the newsroom run itself behaved.",
};

/**
 * Rule-specific explanations, keyed by exact rule id where the id is fixed
 * (e.g. `structure_signal.triad_overuse`) or by the rule family prefix where
 * the tail is data-dependent (`blocked_boilerplate.<slug>`,
 * `review_vocabulary.<word>`, `fact.claim_<status>`).
 */
export const FINDING_RULE_EXPLANATIONS: Record<string, string> = {
  blocked_boilerplate: "A stock phrase was flagged for rewriting without changing the claim.",
  review_vocabulary: "A context-sensitive word was flagged for editorial review.",
  "structure_signal.repeated_openers": "Several sentences open the same way, which reads as mechanical.",
  "structure_signal.triad_overuse": "Rule-of-three constructions appear too often in a row.",
  "structure_signal.uniform_sentence_length": "Sentence lengths are too uniform, which flattens the rhythm.",
  "structure_signal.transition_density": "Transition words are packed too closely together.",
  "readability_signal.passive_voice": "Passive voice leaves the actor unclear.",
  "readability_signal.long_sentence": "The sentence is long enough to slow the reader down.",
  "readability_signal.flesch_score": "Overall readability sits below the target band for this piece.",
  "citation.unknown_label_removed": "A citation label the system does not recognise was removed.",
  "fact.claim_span_unresolved": "A claim could not be matched to the text it was checked against.",
  "fact.evidence_label_not_snapshotted": "A claim cites evidence that was not captured for this run.",
  "fact.quote_mismatch": "A quote in the draft does not match the captured source passage.",
  "fact.single_source_high_stakes": "A high-stakes claim rests on a single source.",
  "fact.claim_supported": "The captured evidence establishes support for this claim.",
  "fact.claim_contradicted": "The captured evidence contradicts this claim.",
  "fact.claim_ambiguous": "The captured evidence does not settle this claim either way.",
  "fact.claim_stale": "The evidence behind this claim changed after it was checked.",
  "fact.claim_unsupported": "No captured evidence establishes support for this claim.",
  "fact.claim_opinion": "This passage states an opinion, so it is not held to factual support.",
  fact: "This finding concerns how a claim in the draft lines up with the captured evidence.",
};

/**
 * Resolves the human-readable explanation for a finding. Prefers the exact
 * rule id, then the rule family (the segment before the first dot), then the
 * category explanation — so an unseen rule id still renders a plain sentence
 * instead of a raw code.
 */
export function findingExplanation(ruleId: string, category: string): string {
  const exact = FINDING_RULE_EXPLANATIONS[ruleId];
  if (exact) return exact;
  const family = FINDING_RULE_EXPLANATIONS[ruleId.split(".")[0] ?? ""];
  if (family) return family;
  return (
    FINDING_CATEGORY_EXPLANATIONS[category] ??
    "This finding reports an issue the newsroom checks flagged in the draft."
  );
}

/**
 * Shown on the findings panel while the editor holds unsaved edits (issue
 * #517 AC15). The findings, claims, and evidence were computed against the
 * last saved revision — never against text still being typed.
 */
export const FINDINGS_STALE_NOTICE =
  "Checks reflect the last saved revision. They do not cover unsaved edits, and saving your edits " +
  "invalidates the affected checks and any Ready approval.";

/**
 * Fact-check status display names.
 *
 * `passed` deliberately stays "Fact-checked": it reports that the fact-check
 * stage RAN against this revision — never that every claim is supported
 * (per-claim support is established separately) and never that a human
 * approved anything. `DraftRevisionDiff` and the promote dialog render this
 * exact string, and the export dialog adds `EXPORT_STATUS_EXPLANATION`
 * beneath it so the distinction is stated where approval decisions happen.
 */
export const FACT_STATUS_LABELS: Record<string, string> = {
  not_run: "Not fact-checked",
  running: "Fact-checking",
  passed: "Fact-checked",
  findings: "Fact-checked with findings",
  invalidated: "Fact-check invalidated",
};

/** Source input role display names. */
export const INPUT_ROLE_LABELS: Record<string, string> = {
  manuscript: "Manuscript",
  reference: "Reference",
  style: "Style sample",
  background: "Background",
  challenge: "Challenge",
};

/** Source authority display names. */
export const INPUT_AUTHORITY_LABELS: Record<string, string> = {
  primary: "Primary",
  official: "Official",
  secondary: "Secondary",
  user_asserted: "User asserted",
  unknown: "Unknown",
};

/** Quality tier display names and the consequence each tier carries at the Ready gate. */
export const TIER_LABELS: Record<string, string> = {
  standard: "Standard",
  high_stakes: "High stakes",
  sensitive: "Sensitive",
};

export const TIER_DESCRIPTIONS: Record<string, string> = {
  standard: "A single-source high-stakes claim is recorded as a warning.",
  high_stakes:
    "A single-source high-stakes claim blocks Ready until it is attributed and waived with a reason.",
  sensitive:
    "A single-source high-stakes claim blocks Ready unless the sole source is the primary official authority for that exact statement.",
};

/** Target-format display names, keyed by the backend `piece_type` values. */
export const PIECE_TYPE_LABELS: Record<string, string> = {
  article: "News article",
  report: "Report",
  brief: "Brief",
  press_release: "Press release",
  other: "Other",
};

/** How far the rewrite may move from the source manuscript. */
export const TRANSFORMATION_STRENGTH_LABELS: Record<string, string> = {
  light: "Light — tighten wording, keep structure",
  moderate: "Moderate — restructure sections, keep every claim",
  substantial: "Substantial — reorganise and rewrite throughout",
};

/** Which source wins when the manuscript and the vault disagree. */
export const DRAFTING_PRIORITY_LABELS: Record<string, string> = {
  manuscript: "Prefer the manuscript",
  vault: "Prefer vault sources",
  balanced: "Balance manuscript and vault",
};

/** Shown when the server reports 503 draft_room_disabled. */
export const DRAFT_ROOM_DISABLED_MESSAGE =
  "Draft Room is not enabled on this deployment. An administrator can turn it on; existing projects stay readable and exportable in the meantime.";

/** Project mode display names and help text. */
export const MODE_LABELS: Record<string, string> = {
  rewrite: "Rewrite existing material",
  compose: "Compose a new piece",
};

export const MODE_DESCRIPTIONS: Record<string, string> = {
  rewrite: "Start from a manuscript you upload and rework it against your vault.",
  compose: "Write a new piece from your brief, your source files, and your vault.",
};

/**
 * Reasons `Mark Ready` can be unavailable, keyed by the backend's 409 error codes plus the
 * client-side preconditions the UI can evaluate before submitting.
 */
export const READY_BLOCKER_LABELS: Record<string, string> = {
  active_job: "A newsroom run is still active for this project.",
  invalid_state: "This project is not awaiting review.",
  not_current_revision: "This is not the current revision.",
  fact_not_current: "This revision has no current fact-check result.",
  fact_candidate_mismatch:
    "The fact-check was run against different text. Run the newsroom again on this revision.",
  non_waivable_blocker: "This revision has factual blockers that cannot be waived.",
  unresolved_blocker: "This revision has unresolved blocking findings.",
  invalid_waiver: "A waiver on this revision is missing an actor, a reason, or a matching rule version.",
  stale_waiver: "A waiver no longer matches the text it was granted for.",
  unresolved_claim_blocker: "This revision has unresolved factual claims.",
  evidence_changed: EVIDENCE_INVALIDATED_WARNING,
  source_deleted: EVIDENCE_INVALIDATED_WARNING,
  source_only_acknowledgment_required: SOURCE_ONLY_WARNING,
  vault_access_revoked: "You no longer have read access to this project's vault.",
  conflict: "This project changed in another tab. Reload and try again.",
};

/** Export filename suffix explanations, shown before download. */
export const EXPORT_UNVERIFIED_EXPLANATION =
  "This revision is not currently fact-checked, so the file is named with an -UNVERIFIED.md suffix.";
export const EXPORT_REVIEW_EXPLANATION =
  "This revision is fact-checked but has not been marked Ready, so the file is named with a -REVIEW.md suffix.";
export const EXPORT_READY_EXPLANATION =
  "This is the approved Ready revision, so the file uses the ordinary project filename.";
export const EXPORT_ACK_LABEL =
  "I understand this revision has not passed a current fact-check.";

/**
 * States the three distinct things the export dialog reports, so a completed
 * fact check cannot be read as per-claim support or as approval (issue #517
 * AC13): fact status says the fact-check stage completed against this
 * revision; "Supported" is established per claim by the captured evidence;
 * Ready is a human approval recorded separately.
 */
export const EXPORT_STATUS_EXPLANATION =
  "Fact status reports that the fact-check stage completed against this revision. Support for each " +
  "claim is established separately by the captured evidence, and Ready is a human approval recorded " +
  "on top of both.";

/**
 * Unresolved-blocker disclosure shown in the export dialog BEFORE the export
 * action is used (issue #517 AC13b). Count is the revision's open blocker
 * findings.
 */
export function exportOpenBlockersText(count: number): string {
  const noun = count === 1 ? "blocker" : "blockers";
  const pronoun = count === 1 ? "it" : "them";
  return `This revision has ${count} unresolved ${noun} finding${count === 1 ? "" : "s"}. ` +
    `Resolve or waive ${pronoun} before this draft can be marked Ready.`;
}

/**
 * Helper copy beside the brief-template controls (issue #517 AC14). Templates
 * carry the brief only — never evidence, fact-check, approval, or readiness
 * state — and compiling re-retrieves sources from scratch.
 */
export const BRIEF_TEMPLATE_HELP =
  "Templates fill the brief fields only. They never carry evidence, fact-check, approval, or " +
  "readiness state, and sources are re-retrieved whenever the newsroom runs.";

/** Save-revision consequence, shown in the confirmation dialog. */
export const SAVE_REVISION_CONSEQUENCE =
  "Saving creates a new immutable revision. The current fact-check result and any Ready approval " +
  "are invalidated, and the newsroom must run again before this revision can be marked Ready.";

/** Promotion consequence, shown in the promote dialog. */
export const PROMOTE_CONSEQUENCE =
  "Promoting copies the selected content into this vault as a new document and starts normal " +
  "indexing. It does not change or index the private Draft Room source or revision.";

/** Cancellation consequence, shown in the cancel confirmation. */
export const CANCEL_CONSEQUENCE =
  "A model call already in flight may finish, but its output is discarded and not saved.";

/** Diff legend text. Additions and removals are never signalled by colour alone. */
export const DIFF_LEGEND = {
  added: "Added",
  removed: "Removed",
  unchanged: "Unchanged",
} as const;

export const DIFF_MARKERS = {
  added: "+",
  removed: "−",
  unchanged: " ",
} as const;
