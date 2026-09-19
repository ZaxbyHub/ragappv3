// frontend/src/lib/followUpSuggestions.ts
// Issue #573 (AC1): derive 2-3 short suggested next questions for a completed
// assistant turn from the context that turn ALREADY retrieved (its sources)
// plus the user's own question — deterministic, no LLM call, and crucially no
// new retrieval call (the issue's constraint).

/** Interrogatives/fillers stripped from the front of the user query so the
 * topic embeds grammatically ("…risks around the coolant interval"). */
const LEADING_FILLERS = new Set([
  "what", "whats", "why", "how", "when", "where", "who", "which",
  "does", "do", "did", "is", "are", "was", "were", "can", "could",
  "should", "would", "will", "shall", "may", "might", "the", "a", "an",
  "please", "tell", "me", "about",
]);

/** Trim to a short topic phrase suitable for embedding in a question. */
function topicFrom(userContent: string): string {
  const cleaned = userContent
    .replace(/\s+/g, " ")
    .replace(/[?.!]+\s*$/, "")
    .trim()
    .replace(/^(please|could you|can you|would you)\s+/i, "")
    .trim();
  if (!cleaned) return "";
  let words = cleaned.split(" ");
  while (words.length > 1 && LEADING_FILLERS.has(words[0].toLowerCase())) {
    words = words.slice(1);
  }
  if (words.length <= 8) return words.join(" ");
  return words.slice(0, 8).join(" ");
}

function truncate(text: string, max: number): string {
  return text.length <= max ? text : `${text.slice(0, max - 1).trimEnd()}…`;
}

/**
 * Derive follow-up suggestions from the turn's already-retrieved context.
 * Returns 0-3 strings; empty when there is nothing to work from.
 */
export function deriveFollowUps(
  userContent: string,
  sourceTitles: string[]
): string[] {
  const topic = topicFrom(userContent ?? "");
  const titles = (sourceTitles ?? [])
    .map((t) => (typeof t === "string" ? t.trim() : ""))
    .filter((t) => t.length > 0)
    .slice(0, 3);

  const candidates: string[] = [];
  if (topic) {
    candidates.push(`What are the key risks around ${topic}?`);
    candidates.push(`Summarize the main findings about ${topic}`);
    candidates.push(`List the open questions about ${topic}`);
  }
  if (titles.length >= 2) {
    candidates.push(
      `Where do ${truncate(titles[0], 40)} and ${truncate(titles[1], 40)} disagree?`
    );
  } else if (titles.length === 1) {
    candidates.push(`What does ${truncate(titles[0], 50)} conclude?`);
  }

  const seen = new Set<string>();
  const unique: string[] = [];
  for (const candidate of candidates) {
    if (candidate.length <= 80 && !seen.has(candidate)) {
      seen.add(candidate);
      unique.push(candidate);
    }
    if (unique.length === 3) break;
  }
  return unique;
}
