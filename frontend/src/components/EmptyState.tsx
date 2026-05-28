import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

interface EmptyStateProps {
  icon?: LucideIcon;
  title: string;
  description?: string;
  action?: ReactNode;
  className?: string;
}

export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  className = "",
}: EmptyStateProps) {
  return (
    <div
      className={`flex flex-col items-center justify-center py-12 text-center text-muted-foreground ${className}`}
      role="status"
      aria-live="polite"
    >
      {Icon && <Icon className="h-12 w-12 mb-4 opacity-30" aria-hidden="true" />}
      <p className="text-lg font-medium text-foreground">{title}</p>
      {description && (
        <p className="text-sm mt-1 max-w-sm">{description}</p>
      )}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}
