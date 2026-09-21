/**
 * External font-origin scanner (issue #640, AC6).
 *
 * The #627 review gap: the external-origin contract was enforced against
 * frontend/index.html only, so a remote @import or url() hidden in any
 * bundled CSS source would pass. This helper applies the same forbidden-
 * origin check to arbitrary file sets (HTML, CSS, anything textual) so the
 * scan can cover the whole style surface.
 */

/** Origins the self-hosting contract forbids (mirrors the #572 AC1 test). */
export const FORBIDDEN_FONT_ORIGINS = [
  "fonts.googleapis.com",
  "fonts.gstatic.com",
] as const;

export interface ScannedFile {
  path: string;
  content: string;
}

/**
 * Return the paths of every file whose content references a forbidden font
 * origin (case-insensitive substring match, same semantics as the #572
 * index.html test). Files are checked independently, so one offender does
 * not mask another.
 */
export function scanForExternalOrigins(files: ScannedFile[]): string[] {
  const offenders: string[] = [];
  for (const file of files) {
    const lowered = file.content.toLowerCase();
    if (FORBIDDEN_FONT_ORIGINS.some((origin) => lowered.includes(origin))) {
      offenders.push(file.path);
    }
  }
  return offenders;
}
