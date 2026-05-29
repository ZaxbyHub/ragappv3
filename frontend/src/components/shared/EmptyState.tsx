import { LucideIcon } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";

interface EmptyStateProps {
  /** Icon to display */
  icon: LucideIcon;
  /** Main message */
  title: string;
  /** Description/help text */
  description?: string;
  /** Optional CTA button */
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

export function EmptyState({ icon: Icon, title, description, action, size = "md" }: EmptyStateProps) {
  return (
    <Card className="flex-shrink-0 w-full flex flex-col flex-1">
      <CardContent className="py-12 text-center flex-1">
        <div className={`${sizeClasses[size]} mx-auto mb-4 rounded-full bg-muted flex items-center justify-center`} aria-hidden="true">
          <Icon className="w-1/2 h-1/2 text-muted-foreground" />
        </div>
        <p className="font-medium text-foreground">{title}</p>
        {description && (
          <p className="text-sm text-muted-foreground mt-1">{description}</p>
        )}
        {action && (
          <Button onClick={action.onClick} className="mt-4">
            {action.label}
          </Button>
        )}
      </CardContent>
    </Card>
  );
}
