import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertOctagon, AlertTriangle, CheckCircle2, Circle, Loader2, XCircle } from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { STAGE_LABELS } from "@/components/draft-room/labels";
import type { DraftJobStatus, DraftStage } from "@/lib/api/draftRoom";
import { cn } from "@/lib/utils";

export interface DraftStageRailProps {
  /** From `capabilities.compile_stage_order`. */
  stageOrder: string[];
  /** Attempts for the active/most recent job. */
  stages: DraftStage[];
  activeStage: string | null;
  selectedStage: string | null;
  onSelectStage: (stage: string) => void;
  jobStatus: DraftJobStatus | null;
  blockerCountsByStage?: Record<string, number>;
  warningCountsByStage?: Record<string, number>;
}

type StageRenderState = "pending" | "running" | "complete" | "warning" | "blocked" | "failed";

const STATE_ICONS: Record<StageRenderState, LucideIcon> = {
  pending: Circle,
  running: Loader2,
  complete: CheckCircle2,
  warning: AlertTriangle,
  blocked: AlertOctagon,
  failed: XCircle,
};

const STATE_CLASSES: Record<StageRenderState, string> = {
  pending: "text-muted-foreground border-border",
  running: "text-primary border-primary/40",
  complete: "text-success border-success/40",
  warning: "text-warning border-warning/40",
  blocked: "text-destructive border-destructive/40",
  failed: "text-destructive border-destructive/40",
};

/** The latest attempt for a stage name — retries share a stage name with a higher `attempt`. */
function latestAttempt(stages: DraftStage[], stageName: string): DraftStage | undefined {
  let latest: DraftStage | undefined;
  for (const entry of stages) {
    if (entry.stage !== stageName) continue;
    if (!latest || entry.attempt > latest.attempt) latest = entry;
  }
  return latest;
}

function deriveState(
  stage: string,
  entry: DraftStage | undefined,
  activeStage: string | null,
  jobStatus: DraftJobStatus | null,
  blockerCount: number,
  warningCount: number,
): StageRenderState {
  if (entry) {
    switch (entry.status) {
      case "completed":
        if (blockerCount > 0) return "blocked";
        if (warningCount > 0) return "warning";
        return "complete";
      case "failed":
      case "cancelled":
        return "failed";
      case "running":
        return "running";
      case "pending":
      case "skipped":
      default:
        return "pending";
    }
  }
  if (stage === activeStage && jobStatus === "running") return "running";
  return "pending";
}

/** Plain-language state description used for the accessible name and visible text. */
function describeState(
  state: StageRenderState,
  blockerCount: number,
  warningCount: number,
  errorCode: string | null | undefined,
): string {
  switch (state) {
    case "pending":
      return "pending";
    case "running":
      return "running";
    case "complete":
      return "complete";
    case "warning":
      return warningCount > 0
        ? `warning, ${warningCount} ${warningCount === 1 ? "warning" : "warnings"}`
        : "warning";
    case "blocked":
      return blockerCount > 0
        ? `blocked, ${blockerCount} ${blockerCount === 1 ? "blocker" : "blockers"}`
        : "blocked";
    case "failed":
      return errorCode ? `failed, ${errorCode}` : "failed";
    default:
      return state;
  }
}

function formatElapsed(startedAt: string | null | undefined, nowMs: number): string | null {
  if (!startedAt) return null;
  const start = new Date(startedAt).getTime();
  if (Number.isNaN(start)) return null;
  const totalSeconds = Math.max(0, Math.floor((nowMs - start) / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${seconds.toString().padStart(2, "0")}`;
}

/**
 * Horizontally-scrollable rail of compile stage buttons. Keyboard operable with arrow-key
 * roving focus (Left/Right, Home/End) and a single tab stop; each button's accessible name
 * states its stage's status so colour is never the only signal.
 */
export function DraftStageRail({
  stageOrder,
  stages,
  activeStage,
  selectedStage,
  onSelectStage,
  jobStatus,
  blockerCountsByStage,
  warningCountsByStage,
}: DraftStageRailProps) {
  const buttonRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const [focusedIndex, setFocusedIndex] = useState(() => Math.max(0, stageOrder.indexOf(selectedStage ?? "")));
  const [announcement, setAnnouncement] = useState("");
  const lastSignatureRef = useRef("");
  const [now, setNow] = useState(() => Date.now());

  const entries = useMemo(
    () =>
      stageOrder.map((stage) => {
        const entry = latestAttempt(stages, stage);
        const blockerCount = blockerCountsByStage?.[stage] ?? 0;
        const warningCount = warningCountsByStage?.[stage] ?? 0;
        const state = deriveState(stage, entry, activeStage, jobStatus, blockerCount, warningCount);
        return { stage, entry, blockerCount, warningCount, state };
      }),
    [stageOrder, stages, activeStage, jobStatus, blockerCountsByStage, warningCountsByStage],
  );

  const hasRunningStage = entries.some((item) => item.state === "running");

  // Tick the elapsed-time display once a second, only while a stage is actually running.
  useEffect(() => {
    if (!hasRunningStage) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [hasRunningStage]);

  // Keep the single tab stop aligned with the caller's current selection.
  useEffect(() => {
    if (selectedStage == null) return;
    const idx = stageOrder.indexOf(selectedStage);
    if (idx >= 0) setFocusedIndex(idx);
  }, [selectedStage, stageOrder]);

  // Announce stage transitions only (never per-tick elapsed time) so the live region can't spam.
  useEffect(() => {
    const signature = entries.map((item) => `${item.stage}:${item.state}`).join("|");
    if (signature === lastSignatureRef.current) return;
    lastSignatureRef.current = signature;
    if (!activeStage) return;
    const active = entries.find((item) => item.stage === activeStage);
    if (!active) return;
    const label = STAGE_LABELS[active.stage] ?? active.stage;
    setAnnouncement(`${label} stage ${describeState(active.state, active.blockerCount, active.warningCount, active.entry?.error_code)}`);
  }, [entries, activeStage]);

  const handleKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLButtonElement>) => {
      const lastIndex = stageOrder.length - 1;
      if (lastIndex < 0) return;
      let nextIndex: number | null = null;
      if (event.key === "ArrowRight") nextIndex = Math.min(focusedIndex + 1, lastIndex);
      else if (event.key === "ArrowLeft") nextIndex = Math.max(focusedIndex - 1, 0);
      else if (event.key === "Home") nextIndex = 0;
      else if (event.key === "End") nextIndex = lastIndex;
      if (nextIndex === null) return;
      event.preventDefault();
      setFocusedIndex(nextIndex);
      buttonRefs.current[stageOrder[nextIndex]]?.focus();
    },
    [focusedIndex, stageOrder],
  );

  return (
    <nav aria-label="Compile stages" className="w-full">
      <div className="overflow-x-auto">
        <ul className="flex min-w-max items-stretch gap-2 p-1">
          {entries.map(({ stage, entry, blockerCount, warningCount, state }, index) => {
            const Icon = STATE_ICONS[state];
            const label = STAGE_LABELS[stage] ?? stage;
            const stateText = describeState(state, blockerCount, warningCount, entry?.error_code);
            const elapsed = state === "running" ? formatElapsed(entry?.started_at, now) : null;
            return (
              <li key={stage} className="shrink-0">
                <button
                  type="button"
                  ref={(node) => {
                    buttonRefs.current[stage] = node;
                  }}
                  tabIndex={index === focusedIndex ? 0 : -1}
                  aria-current={stage === selectedStage ? "step" : undefined}
                  aria-label={`${label}, ${stateText}`}
                  onFocus={() => setFocusedIndex(index)}
                  onKeyDown={handleKeyDown}
                  onClick={() => onSelectStage(stage)}
                  className={cn(
                    "flex flex-col items-center gap-1 rounded-sm border bg-background px-3 py-2 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
                    STATE_CLASSES[state],
                    stage === selectedStage && "bg-accent",
                  )}
                >
                  <Icon
                    aria-hidden="true"
                    className={cn("h-4 w-4", state === "running" && "animate-spin motion-reduce:animate-none")}
                  />
                  <span>{label}</span>
                  {state === "running" && elapsed && (
                    <span aria-hidden="true" className="text-2xs text-muted-foreground">
                      {elapsed}
                    </span>
                  )}
                  {state === "warning" && (
                    <span className="text-2xs">
                      {warningCount} {warningCount === 1 ? "warning" : "warnings"}
                    </span>
                  )}
                  {state === "blocked" && (
                    <span className="text-2xs">
                      {blockerCount} {blockerCount === 1 ? "blocker" : "blockers"}
                    </span>
                  )}
                  {state === "failed" && entry?.error_code && <span className="text-2xs">{entry.error_code}</span>}
                </button>
              </li>
            );
          })}
        </ul>
      </div>
      <div aria-live="polite" className="sr-only">
        {announcement}
      </div>
    </nav>
  );
}
