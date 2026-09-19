// frontend/src/components/chat/FollowUpSuggestions.tsx
// Issue #573 (AC1): suggested next questions after a completed assistant
// turn, derived from that turn's already-retrieved context (no new retrieval
// call). Rendered by TranscriptPane after the newest completed assistant
// message; renders nothing when there are no suggestions.

interface FollowUpSuggestionsProps {
  suggestions: string[];
  onSelect: (suggestion: string) => void;
}

export function FollowUpSuggestions({ suggestions, onSelect }: FollowUpSuggestionsProps) {
  if (suggestions.length === 0) return null;

  return (
    <section
      aria-label="Suggested follow-ups"
      className="mt-3 mb-2"
      data-testid="follow-up-suggestions"
    >
      <div className="flex flex-wrap gap-2">
        {suggestions.map((suggestion) => (
          <button
            key={suggestion}
            type="button"
            onClick={() => onSelect(suggestion)}
            className="rounded-full border border-border bg-card px-3 py-1.5 text-left text-xs text-muted-foreground transition-colors hover:border-primary/40 hover:bg-accent/10 hover:text-foreground focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
          >
            {suggestion}
          </button>
        ))}
      </div>
    </section>
  );
}
