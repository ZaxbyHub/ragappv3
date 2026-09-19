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
  if (words.length <= 8) {
    // Char-cap the topic too: an 8-word topic can still exceed the 80-char
    // suggestion budget and leave zero suggestions (PRR-009 coverage caught
    // this edge).
    return truncate(words.join(" "), 40);
  }
  return truncate(words.slice(0, 8).join(" "), 40);
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

  // Interleave the source-grounded comparison with the topic templates so a
  // present topic never crowds the source-based suggestion out of the 3-slot
  // cap entirely (PRR-009 coverage surfaced this ordering flaw).
  const candidates: string[] = [];
  const comparison =
    titles.length >= 2
      ? `Where do ${truncate(titles[0], 40)} and ${truncate(titles[1], 40)} disagree?`
      : titles.length === 1
        ? `What does ${truncate(titles[0], 50)} conclude?`
        : null;
  if (topic) {
    candidates.push(`What are the key risks around ${topic}?`);
    candidates.push(`Summarize the main findings about ${topic}`);
    if (comparison) candidates.push(comparison);
    candidates.push(`List the open questions about ${topic}`);
  }
  if (comparison && (!topic || candidates[candidates.length - 1] !== comparison)) {
    candidates.push(comparison);
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
