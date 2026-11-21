// frontend/src/lib/followUpSuggestions.ts
// Issue #573 (AC1) / #1153 AC6: derive 2-3 short suggested next questions for
// a completed assistant turn from the context that turn ALREADY produced
// (its answer text and its sources) — deterministic, no LLM call, and
// crucially no new retrieval call (the issue's constraint). The topic comes
// from the ASSISTANT ANSWER (issue #1153 AC6): the answer names what the
// turn actually established, while the user question names what was asked.

/** Interrogatives/fillers stripped from the front of the user query so the
 * topic embeds grammatically ("…risks around the coolant interval"). */
const LEADING_FILLERS = new Set([
  "what", "whats", "why", "how", "when", "where", "who", "which",
  "does", "do", "did", "is", "are", "was", "were", "can", "could",
  "should", "would", "will", "shall", "may", "might", "the", "a", "an",
  "please", "tell", "me", "about",
]);

/** Imperative verbs stripped ONCE from the front of the user query (issue
 * #689 / UI-R1-04): "Explain the vendor process" must not leak its verb into
 * template chips like "What are the key risks around Explain …?". Only the
 * leading verb is removed — the rest of the question stays untouched. */
const LEADING_IMPERATIVES = new Set([
  "explain", "describe", "summarize", "summarise", "list", "show",
  "give", "write", "outline", "identify", "find", "compare",
]);

/** Cap the topic at `max` chars on a word boundary — no trailing ellipsis,
 * which read as mid-phrase truncation inside the chip text (UI-R1-04). A
 * single word longer than `max` has no internal boundary, so it is kept
 * whole rather than hard-cut mid-word; the 80-char candidate filter in
 * deriveFollowUps still bounds every shipped suggestion. */
function capAtWordBoundary(text: string, max: number): string {
  if (text.length <= max) return text;
  const words = text.split(" ");
  let out = "";
  for (const word of words) {
    const candidate = out ? `${out} ${word}` : word;
    if (candidate.length > max) break;
    out = candidate;
  }
  return out || text;
}

/** Trim to a short topic phrase suitable for embedding in a question.
 * Issue #1153 AC6: the input is the ASSISTANT ANSWER text — the topic must
 * name what the turn established, not what the user asked. */
/** Answer-chrome that must not leak into a follow-up topic (review F-05):
 *  citation labels the prompt builder emits (`[S1]`, `[M2]`, `[A1]`), inline
 *  markdown (`**bold**`, `*italic*`, `` `code` ``), and boilerplate openers
 *  ("Based on the documents,", "Here is"). Stripped before the topic phrase
 *  is chosen so a chip reads "coolant interval" not "Based on the documents,
 *  the coolant". */
const ANSWER_OPENERS = [
  "based on the documents",
  "based on the sources",
  "based on the provided",
  "based on this",
  "according to the documents",
  "according to the sources",
  "according to",
  "here is",
  "here are",
  "here's",
];

function stripAnswerChrome(text: string): string {
  return text
    // Citation labels: [S1] [M2] [A1] (and multi-digit).
    .replace(/\[[SMA]\d+\]/gi, "")
    // Inline markdown emphasis / code delimiters.
    .replace(/(\*\*|\*|`)/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function stripLeadingOpener(text: string): string {
  for (const opener of ANSWER_OPENERS) {
    const re = new RegExp(`^${opener}\\s*,?\\s*`, "i");
    const next = text.replace(re, "");
    if (next !== text) return next.trim();
  }
  return text;
}

function topicFrom(assistantAnswer: string): string {
  const cleaned = stripLeadingOpener(
    stripAnswerChrome(assistantAnswer)
      .replace(/[?.!]+\s*$/, "")
      .trim()
      .replace(/^(please|could you|can you|would you)\s+/i, "")
      .trim()
  );
  if (!cleaned) return "";
  let words = cleaned.split(" ");
  // Strip ONE leading imperative verb — even when it is the only word, so a
  // bare "Explain." cannot resurface as "risks around Explain" (PRR-002).
  // After an imperative, skip the leading-filler loop: the following words
  // are the sentence's OBJECT, not interrogative scaffolding, and eating
  // them garbles the chip ("Compare A and B" must keep its "A" — F-5).
  let strippedImperative = false;
  if (
    words.length > 0 &&
    LEADING_IMPERATIVES.has(words[0].toLowerCase())
  ) {
    words = words.slice(1);
    strippedImperative = true;
  }
  if (!strippedImperative) {
    while (words.length > 1 && LEADING_FILLERS.has(words[0].toLowerCase())) {
      words = words.slice(1);
    }
  }
  if (words.length <= 8) {
    // Char-cap the topic too: an 8-word topic can still exceed the 80-char
    // suggestion budget and leave zero suggestions (PRR-009 coverage caught
    // this edge).
    return capAtWordBoundary(words.join(" "), 40);
  }
  return capAtWordBoundary(words.slice(0, 8).join(" "), 40);
}

function truncate(text: string, max: number): string {
  return text.length <= max ? text : `${text.slice(0, max - 1).trimEnd()}…`;
}

/**
 * Derive follow-up suggestions from the turn's already-produced context.
 * Issue #1153 AC6: the FIRST argument is the assistant ANSWER text (the
 * topic source); source titles ground the comparison suggestions. Returns
 * 0-3 strings; empty when there is nothing to work from.
 */
export function deriveFollowUps(
  assistantContent: string,
  sourceTitles: string[]
): string[] {
  const topic = topicFrom(assistantContent ?? "");
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
