import type { CanvasVersionOrigin } from "@/lib/api/canvas";

/** Human labels for the canvas version origin badges (issue #509). */
export const CANVAS_ORIGIN_LABELS: Record<CanvasVersionOrigin, string> = {
  created: "Created",
  user_edit: "Edited",
  model_edit: "Model edit",
  restore: "Restored",
};

/** Bounded-preview label: preview support is explicit, never implied. */
export const CANVAS_PREVIEW_UNSUPPORTED_LABEL = "Preview not supported for this format";

/** Page-local confirmation string for the route-transition guard this page owns. */
export const CANVAS_UNSAVED_CHANGES_WARNING =
  "You have unsaved changes to this canvas. Leaving now will discard them. Continue?";

/** Notice shown when a persisted draft is rehydrated on mount. */
export const CANVAS_DRAFT_RESTORED_NOTICE = "Unsaved edits restored";

/** Shown beside the restore action: history is append-only, nothing is lost. */
export const CANVAS_RESTORE_CONSEQUENCE =
  "Restoring appends a new version with the old content. No version is ever deleted.";

export const CANVAS_EDIT_SELECTION_HINT =
  "Only your selection is changed. Save pending edits first.";

/** Diff legend text. Additions and removals are never signalled by colour alone. */
export const CANVAS_DIFF_LEGEND = {
  added: "Added",
  removed: "Removed",
  unchanged: "Unchanged",
} as const;

export const CANVAS_DIFF_MARKERS = {
  added: "+",
  removed: "−",
  unchanged: " ",
} as const;
