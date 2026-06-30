import { motion, useReducedMotion } from "framer-motion";
import { Search, BookOpen, PenLine, Loader2 } from "lucide-react";

export type Stage = "Searching" | "Reading" | "Drafting";

const STAGE_CONFIG: Record<Stage, { label: string; Icon: React.ComponentType<{ className?: string }> }> = {
  Searching: { label: "Searching", Icon: Search },
  Reading: { label: "Reading", Icon: BookOpen },
  Drafting: { label: "Drafting", Icon: PenLine },
};

interface StageIndicatorProps {
  stage: Stage;
}

export function StageIndicator({ stage }: StageIndicatorProps) {
  const prefersReducedMotion = useReducedMotion();
  const config = STAGE_CONFIG[stage as Stage];

  if (!config) return null;

  const { label, Icon } = config;

  return (
    <motion.div
      initial={prefersReducedMotion === false ? { opacity: 0, y: 4 } : { opacity: 0 }}
      animate={prefersReducedMotion === false ? { opacity: 1, y: 0 } : { opacity: 1 }}
      exit={prefersReducedMotion === false ? { opacity: 0, y: 4 } : { opacity: 0 }}
      transition={{ duration: 0.2 }}
      className="flex gap-3 p-4 bg-muted/30"
      role="status"
      aria-live="polite"
      aria-label={`Pipeline stage: ${label}`}
    >
      {/* Avatar placeholder — mirrors AssistantMessage avatar */}
      <div
        className="flex-shrink-0 w-8 h-8 rounded-full flex items-center justify-center bg-muted"
        aria-hidden="true"
      >
        <Icon className="h-4 w-4 text-muted-foreground" />
      </div>

      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 mb-1">
          <span className="font-semibold text-sm">Assistant</span>
        </div>
        <div className="inline-flex items-center gap-2 rounded-2xl bg-muted px-4 py-2.5">
          {stage === "Searching" && (
            <Loader2 className="h-3.5 w-3.5 text-muted-foreground animate-spin" aria-hidden="true" />
          )}
          <span className="text-sm text-muted-foreground">
            {stage === "Searching" && "Searching"}
            {stage === "Reading" && "Reading"}
            {stage === "Drafting" && "Drafting"}
          </span>
        </div>
      </div>
    </motion.div>
  );
}
