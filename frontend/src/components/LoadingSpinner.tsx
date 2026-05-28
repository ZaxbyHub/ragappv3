import { Loader2 } from "lucide-react";

interface LoadingSpinnerProps {
  label?: string;
  size?: number;
  className?: string;
}

export function LoadingSpinner({
  label = "Loading…",
  size = 24,
  className = "",
}: LoadingSpinnerProps) {
  return (
    <div
      className={`flex flex-col items-center justify-center gap-2 py-8 ${className}`}
      role="status"
      aria-live="polite"
    >
      <Loader2
        className="animate-spin text-muted-foreground"
        style={{ width: size, height: size }}
        aria-hidden="true"
      />
      {label && (
        <span className="text-sm text-muted-foreground">{label}</span>
      )}
      <span className="sr-only">{label}</span>
    </div>
  );
}
