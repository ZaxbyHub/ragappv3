import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { CanvasVersionSummary } from "@/lib/api/canvas";

import { CANVAS_ORIGIN_LABELS } from "./labels";

export interface CanvasVersionRailProps {
  versions: CanvasVersionSummary[];
  selectedVersionNo: number | null;
  currentVersionNo: number | null;
  onSelect(versionNo: number): void;
}

/**
 * Keyboard-focusable version list. Each row is a plain `<button>` (Tab-focus
 * order, Enter/Space activate) showing version_no, optional name, a human
 * origin badge, and created_at. Add/removal semantics are never colour-only:
 * the badge text carries the meaning.
 */
export function CanvasVersionRail({
  versions,
  selectedVersionNo,
  currentVersionNo,
  onSelect,
}: CanvasVersionRailProps) {
  return (
    <nav aria-label="Canvas versions" className="flex flex-col gap-1">
      {versions.map((version) => {
        const isSelected = version.version_no === selectedVersionNo;
        const isCurrent = version.version_no === currentVersionNo;
        return (
          <button
            key={version.version_no}
            type="button"
            onClick={() => onSelect(version.version_no)}
            aria-current={isSelected ? "true" : undefined}
            aria-label={`View version ${version.version_no}`}
            data-testid="canvas-version-button"
            data-version-no={version.version_no}
            className={cn(
              "flex flex-col items-start gap-1 rounded-sm border border-border px-3 py-2 text-left text-sm transition-colors",
              "hover:bg-accent focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring",
              isSelected && "border-primary/60 bg-accent"
            )}
          >
            <span className="flex w-full items-center justify-between gap-2">
              <span className="font-medium">
                Version {version.version_no}
                {version.name ? ` — ${version.name}` : ""}
              </span>
              {isCurrent && (
                <Badge variant="outline" className="shrink-0">
                  Current
                </Badge>
              )}
            </span>
            <span className="flex items-center gap-1.5">
              <Badge
                variant="secondary"
                data-testid="canvas-origin-badge"
                data-origin={version.origin}
              >
                {CANVAS_ORIGIN_LABELS[version.origin] ?? version.origin}
              </Badge>
              <span className="text-xs text-muted-foreground">
                {new Date(version.created_at).toLocaleString()}
              </span>
            </span>
          </button>
        );
      })}
    </nav>
  );
}
