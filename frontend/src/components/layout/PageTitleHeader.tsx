import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

interface PageTitleHeaderProps {
  title: ReactNode;
  description?: ReactNode;
  /** Header action controls (buttons, selectors). Rendered in a wrapping
   *  action row so every header stays reachable at 320-768px (issue #776,
   *  UI-R2-02/UI-R4-06: the wrap rule lives here once, not per page). */
  actions?: ReactNode;
  /** Leading control rendered before the title (back button, section icon). */
  before?: ReactNode;
  /** Render only the visually-hidden h1 landmark (e.g. chat's toolbar
   *  header) without the title chip or row chrome. In this mode
   *  `description`/`actions`/`before`/`className` are intentionally not
   *  rendered (the landmark is bare); only `title` and `id` apply. */
  srOnly?: boolean;
  /** Forwarded to the h1 (heading anchors such as canvas-page-heading). */
  id?: string;
  className?: string;
}

export function PageTitleHeader({ title, description, actions, before, srOnly, id, className }: PageTitleHeaderProps) {
  if (srOnly) {
    return (
      <h1 id={id} className="sr-only">
        {title}
      </h1>
    );
  }
  return (
    <div className={cn("flex flex-wrap items-center justify-between gap-x-4 gap-y-2", className)}>
      {before}
      <div className="flex min-w-0 max-w-full flex-col items-start justify-start gap-1 py-1.5 px-6 bg-accent/50 rounded-sm">
        <h1 id={id} className="w-full text-3xl font-semibold tracking-tight [overflow-wrap:anywhere]">
          {title}
        </h1>
        {description && <p className="text-muted-foreground mt-1 font-normal">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}
