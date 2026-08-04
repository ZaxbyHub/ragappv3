import { motion, useReducedMotion } from "framer-motion";

export function WaitingIndicator() {
  const prefersReducedMotion = useReducedMotion();

  return (
    <motion.div
      initial={prefersReducedMotion === false ? { opacity: 0, y: 4 } : { opacity: 0 }}
      animate={prefersReducedMotion === false ? { opacity: 1, y: 0 } : { opacity: 1 }}
      exit={prefersReducedMotion === false ? { opacity: 0, y: 4 } : { opacity: 0 }}
      transition={{ duration: 0.2 }}
      className="flex gap-3 p-4 bg-muted/30"
      role="status"
      aria-label="Waiting for response"
    >
      {/* Avatar placeholder — matches AssistantMessage avatar */}
      <div
        className="shrink-0 w-8 h-8 rounded-full flex items-center justify-center bg-muted"
        aria-hidden="true"
      >
        <svg
          className="h-4 w-4 text-muted-foreground"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="M12 8V4H8" />
          <rect width="16" height="12" x="4" y="8" rx="2" />
          <path d="M2 14h2" />
          <path d="M20 14h2" />
          <path d="M15 13v2" />
          <path d="M9 13v2" />
        </svg>
      </div>

      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 mb-1">
          <span className="font-semibold text-sm">Assistant</span>
        </div>
        <div className="inline-flex items-center gap-1.5 rounded-2xl bg-muted px-4 py-2.5">
          <span className="sr-only">Thinking</span>
          <span
            className="h-2 w-2 rounded-full bg-muted-foreground/60 animate-bounce"
            style={{ animationDelay: "0ms" }}
            aria-hidden="true"
          />
          <span
            className="h-2 w-2 rounded-full bg-muted-foreground/60 animate-bounce"
            style={{ animationDelay: "150ms" }}
            aria-hidden="true"
          />
          <span
            className="h-2 w-2 rounded-full bg-muted-foreground/60 animate-bounce"
            style={{ animationDelay: "300ms" }}
            aria-hidden="true"
          />
        </div>
      </div>
    </motion.div>
  );
}
