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

/** Fact-check status display names. */
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
