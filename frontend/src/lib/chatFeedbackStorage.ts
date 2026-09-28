// frontend/src/lib/chatFeedbackStorage.ts
// Issue #685 (T1-13-S-07 / T1-13-K-07): the per-message feedback mirror in
// localStorage is BEST-EFFORT — reads and writes are guarded so a hostile or
// unavailable storage implementation can never break a durable flow — and
// BOUNDED: at most FEEDBACK_KEY_LIMIT chat_feedback_* keys persist, the
// numerically-oldest ids removed first.

const FEEDBACK_KEY_PREFIX = "chat_feedback_";
export const FEEDBACK_KEY_LIMIT = 1000;

export type FeedbackVote = "up" | "down";

export function feedbackKey(messageId: string): string {
  return `${FEEDBACK_KEY_PREFIX}${messageId}`;
}

export function readFeedbackVote(messageId: string): FeedbackVote | null {
  try {
    const stored = localStorage.getItem(feedbackKey(messageId));
    return stored === "up" || stored === "down" ? stored : null;
  } catch {
    return null;
  }
}

export function writeFeedbackVote(messageId: string, value: FeedbackVote | null): void {
  try {
    const key = feedbackKey(messageId);
    if (value === null) {
      localStorage.removeItem(key);
    } else {
      localStorage.setItem(key, value);
    }
  } catch {
    // Best-effort mirror: a storage failure must never surface in the vote flow.
    return;
  }
  pruneFeedbackKeys();
}

// Strict total order over chat_feedback_* keys: numeric suffixes ascending
// (server ids are AUTOINCREMENT, so a smaller suffix is an older vote target);
// non-numeric suffixes (local Date.now() ids are numeric too, but any legacy
// or malformed suffix sorts after) order lexicographically. Transitive and
// deterministic, so pruning is stable across runs.
function compareFeedbackKeys(a: string, b: string): number {
  const na = a.slice(FEEDBACK_KEY_PREFIX.length);
  const nb = b.slice(FEEDBACK_KEY_PREFIX.length);
  const aNum = /^\d+$/.test(na);
  const bNum = /^\d+$/.test(nb);
  if (aNum && bNum) {
    // Decimal-string comparison: shorter digit strings are smaller numbers;
    // equal lengths compare digit by digit. Avoids Number() precision loss on
    // very long ids.
    if (na.length !== nb.length) return na.length - nb.length;
    return na < nb ? -1 : na > nb ? 1 : 0;
  }
  if (aNum) return -1;
  if (bNum) return 1;
  return na < nb ? -1 : na > nb ? 1 : 0;
}

export function pruneFeedbackKeys(limit: number = FEEDBACK_KEY_LIMIT): number {
  let keys: string[] = [];
  try {
    // Enumerate the LIVE key set through the storage interface only: other
    // tabs (and functional test doubles) may hold keys no local registry
    // knows about, and Object.keys(localStorage) is not a real enumeration.
    const total = localStorage.length;
    for (let i = 0; i < total; i++) {
      const key = localStorage.key(i);
      if (key && key.startsWith(FEEDBACK_KEY_PREFIX)) keys.push(key);
    }
  } catch {
    return 0;
  }
  if (keys.length <= limit) return 0;
  keys = keys.sort(compareFeedbackKeys);
  let removed = 0;
  for (const key of keys.slice(0, keys.length - limit)) {
    try {
      localStorage.removeItem(key);
      removed += 1;
    } catch {
      // best-effort: an unremovable key just leaves the count marginally high
    }
  }
  return removed;
}
