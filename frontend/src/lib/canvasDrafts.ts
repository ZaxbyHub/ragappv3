/**
 * Canvas draft persistence scheme (moved out of CanvasPage.tsx for issue
 * #1032 so the identity-switch reset and the canvas editor share one source
 * of truth for the storage keys — the chatDrafts.ts precedent).
 *
 * Canvas drafts are unsaved, potentially private text keyed per PRINCIPAL
 * and artifact. They must never survive an identity boundary: the boundary
 * reset invalidates every not-yet-executed writer through the generation
 * counter (a pending debounce timer or an unmount/artifact-switch flush
 * scheduled before the switch can never re-create a cleared key
 * afterwards), then removes every `canvas-draft:` key.
 *
 * The principal segment is load-bearing, not cosmetic (chatDrafts.ts
 * precedent): a best-effort `removeItem` can fail (storage surface), and a
 * key that survives removal must still be unreadable by the NEXT principal
 * — keying by principal makes the namespace itself the boundary.
 */

export const CANVAS_DRAFT_PREFIX = "canvas-draft:";

let canvasDraftGeneration = 0;

export const canvasDraftKey = (
  artifactUid: string,
  principalId: number | string | null,
) => `${CANVAS_DRAFT_PREFIX}${principalId ?? "anon"}:${artifactUid}`;

/** Generation captured when a (possibly delayed) draft write is scheduled. */
export const currentCanvasDraftGeneration = (): number => canvasDraftGeneration;

/**
 * Invalidate every not-yet-executed canvas draft write. Writers compare
 * their schedule-time-captured generation against the current one at
 * execution time and drop the write when they differ (plan-critic round 1:
 * capturing at execution time would accept post-boundary writes).
 */
export const invalidatePendingCanvasDraftWrites = (): void => {
  canvasDraftGeneration += 1;
};

export const readCanvasDraft = (
  artifactUid: string,
  principalId: number | string | null,
): string | null => {
  try {
    return localStorage.getItem(canvasDraftKey(artifactUid, principalId));
  } catch {
    return null;
  }
};

export const writeCanvasDraft = (
  artifactUid: string,
  principalId: number | string | null,
  content: string,
  capturedGeneration: number,
): void => {
  if (capturedGeneration !== canvasDraftGeneration) return;
  try {
    localStorage.setItem(canvasDraftKey(artifactUid, principalId), content);
  } catch {
    // Storage unavailable (private mode / quota) — best-effort only.
  }
};

export const clearCanvasDraft = (
  artifactUid: string,
  principalId: number | string | null,
): void => {
  try {
    localStorage.removeItem(canvasDraftKey(artifactUid, principalId));
  } catch {
    // Ignore — nothing persisted means nothing to clear.
  }
};

/**
 * Remove every persisted canvas draft (all principals — which also sweeps
 * any pre-#1032 unscoped `canvas-draft:<uid>` residue). Each removal is
 * attempted independently: one throwing `removeItem` must not abort the
 * remaining keys. Returns the number of keys that could NOT be removed, or
 * **-1 when the storage enumeration itself failed** (wipe completeness
 * unknown — the caller must log that the residual is unobservable instead of
 * reading -1 as success; multi-stage review finding). Keys that survive
 * either failure mode stay unreadable to the next principal because the key
 * namespace is principal-scoped.
 */
export const clearAllCanvasDrafts = (): number => {
  if (typeof window === "undefined") return 0;
  const doomed: string[] = [];
  try {
    for (let i = 0; i < localStorage.length; i += 1) {
      const key = localStorage.key(i);
      if (key && key.startsWith(CANVAS_DRAFT_PREFIX)) doomed.push(key);
    }
  } catch {
    return -1;
  }
  let failures = 0;
  for (const key of doomed) {
    try {
      localStorage.removeItem(key);
    } catch {
      failures += 1;
    }
  }
  return failures;
};
