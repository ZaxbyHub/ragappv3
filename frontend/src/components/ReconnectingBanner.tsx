// frontend/src/components/ReconnectingBanner.tsx
/**
 * FR-019: Prominent reconnecting banner
 *
 * Appears when backend or chat service is unavailable, informing the user
 * and indicating reconnecting state. Auto-dismisses when services recover.
 * Uses aria-live so screen readers announce the state change.
 */
import { cn } from "@/lib/utils";
import { Loader2, WifiOff } from "lucide-react";
import type { HealthStatus } from "@/types/health";

function ReconnectingBanner({ health }: { health: HealthStatus }) {
  const isVisible = !health.loading && (!health.backend || !health.chat);

  if (!isVisible) return null;

  const isBackendDown = !health.backend;

  // Severity: red if backend is down (core API unreachable), amber if only chat is down
  const isSevere = isBackendDown;

  return (
    <div
      role="alert"
      aria-live="polite"
      aria-atomic="true"
      className={cn(
        "sticky top-0 z-50 flex w-full items-center justify-center gap-2 px-4 py-2.5 text-sm font-medium shadow-sm",
        "transition-colors duration-200",
        isSevere
          ? "bg-destructive/95 text-destructive-foreground"
          : "bg-warning/95 text-warning-foreground"
      )}
    >
      {isSevere ? (
        <WifiOff className="h-4 w-4 shrink-0" aria-hidden="true" />
      ) : (
        <Loader2
          className="h-4 w-4 shrink-0 animate-spin"
          aria-hidden="true"
        />
      )}

      <span>
        {isBackendDown
          ? "Connection lost — reconnecting..."
          : "Chat service unavailable — attempting to reconnect..."}
      </span>
    </div>
  );
}

export default ReconnectingBanner;
