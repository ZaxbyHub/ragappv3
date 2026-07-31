/**
 * Pure helpers for clamping resize-handle widths. Shared between the
 * keyboard, mouse, and touch handlers in DocumentsPage so the clamp
 * semantics are testable without rendering the page.
 *
 * The values MUST match the aria-valuemin/aria-valuemax attributes on the
 * DocumentTable resize separator — see DocumentTable.tsx.
 */

export const FILENAME_COL_WIDTH_MIN = 120;
export const FILENAME_COL_WIDTH_MAX = 600;

/** Clamp a filename-column width to the legal [MIN, MAX] range. */
export function clampFilenameColWidth(width: number): number {
  return Math.max(FILENAME_COL_WIDTH_MIN, Math.min(FILENAME_COL_WIDTH_MAX, width));
}
