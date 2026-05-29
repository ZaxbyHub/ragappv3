import { cn } from "@/lib/utils";

function Skeleton({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      // bg-muted alone reads heavy in dark mode; lower the opacity in dark
      // and slightly soften light so skeletons read as scaffolding rather
      // than filled content.
      className={cn("animate-pulse rounded-sm bg-muted/60 dark:bg-muted/40", className)}
      role="status"
      aria-label="Loading..."
      aria-hidden="false"
      {...props}
    />
  );
}

export { Skeleton };
