import { useState, useCallback, useEffect } from "react";
import {
  Copy,
  Check,
  RotateCcw,
  Bug,
  ThumbsUp,
  ThumbsDown,
  AlertCircle,
  GitBranch,
  Pencil,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { updateMessageFeedback } from "@/lib/api";

// =============================================================================
// CopyButton with toast feedback
// =============================================================================

// WCAG 2.5.5 / 2.5.8 touch-target size: the visible button is h-9 w-9 (36px)
// and an absolutely-positioned ::before extends the clickable area to ≥44px on
// both axes without enlarging the visual glyph, keeping the dense action row
// compact while meeting the minimum target size. (UI-D3-01)
const TOUCH_TARGET_44 =
  "relative h-9 w-9 active:scale-95 before:absolute before:left-1/2 before:top-1/2 before:h-11 before:w-11 before:-translate-x-1/2 before:-translate-y-1/2 before:content-['']";

interface CopyActionProps {
  content: string;
  /** If true, strip [S1] / [Source:…] markers before copying */
  stripCitations?: boolean;
  onCopy?: () => void;
}

function CopyAction({ content, stripCitations = false, onCopy }: CopyActionProps) {
  const [state, setState] = useState<"idle" | "copied" | "error">("idle");

  const handleCopy = useCallback(async () => {
    const text = stripCitations
      ? content.replace(/\[Source:[^\]]+\]/g, "").replace(/\[S\d+\]/g, "").trim()
      : content;
    try {
      if (navigator.clipboard) {
        await navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.cssText = "position:fixed;opacity:0;pointer-events:none";
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        const ok = document.execCommand("copy");
        document.body.removeChild(ta);
        if (!ok) throw new Error("execCommand failed");
      }
      setState("copied");
      onCopy?.();
      setTimeout(() => setState("idle"), 2000);
    } catch {
      setState("error");
      toast.error("Couldn't copy to clipboard");
      setTimeout(() => setState("idle"), 2000);
    }
  }, [content, stripCitations, onCopy]);

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className={TOUCH_TARGET_44}
          onClick={handleCopy}
          aria-label={state === "copied" ? "Copied to clipboard" : state === "error" ? "Copy failed" : "Copy message"}
        >
          {state === "copied" ? (
            <Check className="h-3.5 w-3.5 text-success" />
          ) : state === "error" ? (
            <AlertCircle className="h-3.5 w-3.5 text-destructive" />
          ) : (
            <Copy className="h-3.5 w-3.5" />
          )}
        </Button>
      </TooltipTrigger>
      <TooltipContent>
        <p>{state === "copied" ? "Copied!" : state === "error" ? "Copy failed" : "Copy"}</p>
      </TooltipContent>
    </Tooltip>
  );
}

// =============================================================================
// FeedbackAction
// =============================================================================

interface FeedbackActionProps {
  messageId?: string;
  sessionId?: string;
  externalFeedback?: "up" | "down" | null;
  serverFeedback?: "up" | "down" | null;
  onFeedback?: (feedback: "up" | "down" | null) => void;
}

function FeedbackActions({
  messageId,
  sessionId,
  externalFeedback,
  serverFeedback,
  onFeedback,
}: FeedbackActionProps) {
  const [internalFeedback, setInternalFeedback] = useState<"up" | "down" | null>(null);

  useEffect(() => {
    if (serverFeedback !== undefined) {
      setInternalFeedback(serverFeedback);
      if (serverFeedback === null && messageId) {
        try {
          localStorage.removeItem(`chat_feedback_${messageId}`);
        } catch { /* ignore */ }
      }
      return;
    }
    if (!messageId) {
      setInternalFeedback(null);
      return;
    }
    try {
      const stored = localStorage.getItem(`chat_feedback_${messageId}`);
      if (stored === "up" || stored === "down") setInternalFeedback(stored);
      else setInternalFeedback(null);
    } catch { /* ignore */ }
  }, [messageId, serverFeedback]);

  const current = externalFeedback !== undefined ? externalFeedback : internalFeedback;

  const handleFeedback = useCallback(
    (type: "up" | "down") => {
      const prev = current;
      const next: "up" | "down" | null = current === type ? null : type;

      if (externalFeedback === undefined) setInternalFeedback(next);

      try {
        if (messageId) {
          if (next === null) {
            localStorage.removeItem(`chat_feedback_${messageId}`);
          } else {
            localStorage.setItem(`chat_feedback_${messageId}`, next);
          }
        }
      } catch { /* ignore */ }

      onFeedback?.(next);

      if (sessionId && messageId && !isNaN(Number(messageId))) {
        updateMessageFeedback(Number(sessionId), Number(messageId), next).catch(() => {
          if (externalFeedback === undefined) setInternalFeedback(prev);
          try {
            if (messageId) {
              if (prev === null) localStorage.removeItem(`chat_feedback_${messageId}`);
              else localStorage.setItem(`chat_feedback_${messageId}`, prev);
            }
          } catch { /* ignore */ }
          onFeedback?.(prev);
          toast.error("Couldn't save feedback");
        });
      }
    },
    [current, externalFeedback, messageId, sessionId, onFeedback]
  );

  return (
    <>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            className={cn(
              "transition-all duration-150 " + TOUCH_TARGET_44,
              current === "up" && "bg-accent text-accent-foreground scale-105"
            )}
            onClick={() => handleFeedback("up")}
            aria-label="Good response"
            aria-pressed={current === "up"}
          >
            <ThumbsUp className="h-3.5 w-3.5" />
          </Button>
        </TooltipTrigger>
        <TooltipContent><p>Good response</p></TooltipContent>
      </Tooltip>

      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            className={cn(
              "transition-all duration-150 " + TOUCH_TARGET_44,
              current === "down" && "bg-accent text-accent-foreground scale-105"
            )}
            onClick={() => handleFeedback("down")}
            aria-label="Bad response"
            aria-pressed={current === "down"}
          >
            <ThumbsDown className="h-3.5 w-3.5" />
          </Button>
        </TooltipTrigger>
        <TooltipContent><p>Bad response</p></TooltipContent>
      </Tooltip>
    </>
  );
}

// =============================================================================
// AssistantMessageActions
// =============================================================================

interface AssistantMessageActionsProps {
  content: string;
  onRetry?: () => void;
  onFork?: () => void;
  onDebugToggle?: () => void;
  isDebugActive?: boolean;
  showDebug?: boolean;
  messageId?: string;
  sessionId?: string;
  externalFeedback?: "up" | "down" | null;
  serverFeedback?: "up" | "down" | null;
  onFeedback?: (feedback: "up" | "down" | null) => void;
  onCopy?: () => void;
}

export function AssistantMessageActions({
  content,
  onRetry,
  onFork,
  onDebugToggle,
  isDebugActive = false,
  showDebug = true,
  messageId,
  sessionId,
  externalFeedback,
  serverFeedback,
  onFeedback,
  onCopy,
}: AssistantMessageActionsProps) {
  return (
    <div className="flex items-center gap-0.5 mt-3 opacity-60 group-hover:opacity-100 focus-within:opacity-100 [@media(pointer:coarse)]:opacity-100 transition-opacity duration-200">
      <TooltipProvider>
        <CopyAction content={content} stripCitations onCopy={onCopy} />

        {onRetry && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className={TOUCH_TARGET_44}
                onClick={onRetry}
                aria-label="Retry"
              >
                <RotateCcw className="h-3.5 w-3.5" />
              </Button>
            </TooltipTrigger>
            <TooltipContent><p>Regenerate</p></TooltipContent>
          </Tooltip>
        )}

        {onFork && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className={TOUCH_TARGET_44}
                onClick={onFork}
                aria-label="Branch conversation from here"
              >
                <GitBranch className="h-3.5 w-3.5" />
              </Button>
            </TooltipTrigger>
            <TooltipContent><p>Branch from here</p></TooltipContent>
          </Tooltip>
        )}

        {import.meta.env.DEV && showDebug && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className={cn(TOUCH_TARGET_44, isDebugActive && "bg-accent text-accent-foreground")}
                onClick={onDebugToggle}
                aria-label="Toggle debug info"
              >
                <Bug className="h-3.5 w-3.5" />
              </Button>
            </TooltipTrigger>
            <TooltipContent><p>Debug</p></TooltipContent>
          </Tooltip>
        )}

        <FeedbackActions
          messageId={messageId}
          sessionId={sessionId}
          externalFeedback={externalFeedback}
          serverFeedback={serverFeedback}
          onFeedback={onFeedback}
        />
      </TooltipProvider>
    </div>
  );
}

// =============================================================================
// UserMessageActions
// =============================================================================

interface UserMessageActionsProps {
  content: string;
  onEdit?: () => void;
  isEditDisabled?: boolean;
  onFork?: () => void;
}

export function UserMessageActions({
  content,
  onEdit,
  isEditDisabled = false,
  onFork,
}: UserMessageActionsProps) {
  return (
    <div className="flex items-center gap-0.5 mt-2 opacity-0 group-hover:opacity-100 focus-within:opacity-100 [@media(pointer:coarse)]:opacity-100 transition-opacity duration-200">
      <TooltipProvider>
        <CopyAction content={content} />

        {onEdit && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className={TOUCH_TARGET_44}
                onClick={onEdit}
                disabled={isEditDisabled}
                aria-label="Edit message"
              >
                <Pencil className="h-3.5 w-3.5" />
              </Button>
            </TooltipTrigger>
            <TooltipContent><p>{isEditDisabled ? "Editing disabled while generating" : "Edit"}</p></TooltipContent>
          </Tooltip>
        )}

        {onFork && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className={TOUCH_TARGET_44}
                onClick={onFork}
                aria-label="Branch conversation from here"
              >
                <GitBranch className="h-3.5 w-3.5" />
              </Button>
            </TooltipTrigger>
            <TooltipContent><p>Branch from here</p></TooltipContent>
          </Tooltip>
        )}
      </TooltipProvider>
    </div>
  );
}
