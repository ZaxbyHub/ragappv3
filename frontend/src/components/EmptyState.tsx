import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";

interface EmptyStateAction {
  label: string;
  onClick: () => void;
}

interface EmptyStateProps {
  icon?: LucideIcon;
  title: string;
  description?: string;
  action?: ReactNode | EmptyStateAction;
  className?: string;
  size?: "sm" | "md" | "lg";
}

const sizeClasses = {
  sm: "w-8 h-8",
  md: "w-12 h-12",
  lg: "w-16 h-16",
};

export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  className = "",
  size = "md",
}: EmptyStateProps) {
  return (
    <div
      className="flex-shrink-0 w-full flex flex-col flex-1"
      role="status"
      aria-live="polite"
    >
      <Card className="flex-shrink-0 w-full flex flex-col flex-1">
        <CardContent className={`py-12 text-center flex-1 ${className}`}>
          {Icon && (
            <div
              className={`${sizeClasses[size]} mx-auto mb-4 rounded-full bg-muted flex items-center justify-center`}
              aria-hidden="true"
            >
              <Icon className="w-1/2 h-1/2 text-muted-foreground" />
            </div>
          )}
          <p className="font-medium text-foreground">{title}</p>
          {description && (
            <p className="text-sm text-muted-foreground mt-1">{description}</p>
          )}
          {action && (
            <div className="mt-4">
              {isActionObject(action) ? (
                <Button onClick={action.onClick}>{action.label}</Button>
              ) : (
                action
              )}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function isActionObject(action: ReactNode | EmptyStateAction): action is EmptyStateAction {
  return (
    typeof action === "object" &&
    action !== null &&
    "label" in action &&
    "onClick" in action
  );
}
