import { AlertTriangle, LucideIcon } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";

interface ErrorStateProps {
  /** Icon to display (defaults to a warning triangle) */
  icon?: LucideIcon;
  /** Main message */
  title?: string;
  /** Description/help text */
  description?: string;
  /** Optional retry button */
  action?: {
    label: string;
    onClick: () => void;
  };
  /** Icon size variant */
  size?: "sm" | "md" | "lg";
}

const sizeClasses = {
  sm: "w-8 h-8",
  md: "w-12 h-12",
  lg: "w-16 h-16",
};

/**
 * Standardized error display, the error-state sibling of {@link EmptyState}.
 * Uses destructive styling and announces itself via role="alert".
 */
export function ErrorState({
  icon: Icon = AlertTriangle,
  title = "Something went wrong",
  description = "An unexpected error occurred. Please try again.",
  action,
  size = "md",
}: ErrorStateProps) {
  return (
    <Card className="flex-shrink-0 w-full flex flex-col flex-1">
      <CardContent className="py-12 text-center flex-1" role="alert">
        <div
          className={`${sizeClasses[size]} mx-auto mb-4 rounded-full bg-destructive/10 flex items-center justify-center`}
          aria-hidden="true"
        >
          <Icon className="w-1/2 h-1/2 text-destructive" />
        </div>
        <p className="font-medium text-foreground">{title}</p>
        {description && (
          <p className="text-sm text-muted-foreground mt-1">{description}</p>
        )}
        {action && (
          <Button variant="destructive" onClick={action.onClick} className="mt-4">
            {action.label}
          </Button>
        )}
      </CardContent>
    </Card>
  );
}
