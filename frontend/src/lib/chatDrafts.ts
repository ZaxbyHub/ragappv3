/**
 * Chat composer draft persistence scheme (moved out of Composer.tsx for
 * issue #823 so the identity-switch reset and the composer share one source
 * of truth for the storage keys).
 *
 * Drafts are unsent, potentially private text keyed per chat session (or
 * "new"). They must never survive an identity boundary: `logout` and a
 * different-principal `login` clear every stored draft AND invalidate every
 * in-flight writer through the generation counter, so a pending debounce
 * timer or an unmount flush scheduled before the switch can never re-create
 * a cleared key afterwards (issue #823, plan-critic round 3).
 */

export const DRAFT_PREFIX = "ragapp_chat_draft_";

let draftGeneration = 0;

export const getDraftKey = (sessionId: string | null) =>
  `${DRAFT_PREFIX}${sessionId ?? "new"}`;

/** Generation captured when a (possibly delayed) draft write is scheduled. */
export const currentDraftGeneration = (): number => draftGeneration;

/**
 * Invalidate every not-yet-executed draft write. Writers compare their
 * captured generation against the current one at execution time and drop
 * the write when they differ.
 */
export const invalidatePendingDraftWrites = (): void => {
  draftGeneration += 1;
};

/**
 * Remove every persisted chat draft (all sessions + the "new" key).
 *
 * Each removal is attempted independently: one throwing `removeItem` (a
 * storage-surface failure) must not abort the remaining keys and leave the
 * previous identity's draft text behind while the caller proceeds as if the
 * wipe succeeded (review PRR-008).
 */
export const clearAllChatDrafts = (): void => {
  if (typeof window === "undefined") return;
  const doomed: string[] = [];
  try {
    for (let i = 0; i < localStorage.length; i += 1) {
      const key = localStorage.key(i);
      if (key && key.startsWith(DRAFT_PREFIX)) doomed.push(key);
    }
  } catch {
    /* storage unavailable — nothing to enumerate */
    return;
  }
  for (const key of doomed) {
    try {
      localStorage.removeItem(key);
    } catch {
      /* this key could not be removed; keep clearing the rest */
    }
  }
};
