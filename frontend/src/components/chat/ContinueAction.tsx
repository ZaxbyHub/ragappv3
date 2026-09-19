// frontend/src/components/chat/ContinueAction.tsx
// Issue #573 (AC2): "Continue" affordance for assistant responses truncated
// at the length limit. The backend reports llm_metrics.finish_reason on the
// SSE done event; when it is "length" the parent renders this action. Invoking
// it asks the parent to resend a follow-up request that carries the truncated
// content as prior context (see TranscriptPane's handleContinue).

interface ContinueActionProps {
  /** The truncated partial assistant content the continuation resumes from. */
  content: string;
  onContinue: (payload: { content: string }) => void;
}

export function ContinueAction({ content, onContinue }: ContinueActionProps) {
  return (
    <div className="mt-3">
      <button
        type="button"
        onClick={() => onContinue({ content })}
        className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-3 py-1.5 text-xs font-medium text-foreground transition-colors hover:border-primary/40 hover:bg-accent/10 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
        aria-label="Continue generating the truncated response"
      >
        <span aria-hidden>↪</span>
        Continue
        <span className="sr-only">
          — the response stopped at the length limit; continue from the truncated content
        </span>
      </button>
    </div>
  );
}
