// frontend/src/components/chat/VersionStepper.tsx
// Issue #573 (AC3): inline "1 / N" stepper over the client-side edit-version
// snapshots for an edited turn. Editing truncates the session in place and
// re-sends; the pre-edit content is snapshotted in useChatStore
// (messageEditVersions), so sibling versions are navigable without any change
// to the fork/lineage data model. The parent owns which version is displayed
// (stepping swaps it via updateMessage — display-only).

interface VersionStepperProps {
  versions: Array<{ label: string; content: string }>;
  activeIndex: number;
  onSelectIndex: (index: number) => void;
}

export function VersionStepper({ versions, activeIndex, onSelectIndex }: VersionStepperProps) {
  if (versions.length < 2) return null;
  const clampedIndex = Math.min(Math.max(activeIndex, 0), versions.length - 1);

  return (
    <div
      className="mt-1.5 inline-flex items-center gap-1 rounded-md border border-border bg-card px-1.5 py-1 text-xs text-muted-foreground"
      role="group"
      aria-label={`Message versions, version ${clampedIndex + 1} of ${versions.length}: ${versions[clampedIndex].label}`}
      data-active-version={clampedIndex + 1}
    >
      <button
        type="button"
        onClick={() => onSelectIndex(Math.max(0, clampedIndex - 1))}
        aria-label="Show previous version"
        className="inline-flex h-6 w-6 items-center justify-center rounded-sm transition-colors hover:bg-accent/10 hover:text-foreground focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
      >
        <span aria-hidden>‹</span>
      </button>
      <span className="min-w-[3rem] text-center font-mono" aria-hidden>
        {clampedIndex + 1} / {versions.length}
      </span>
      <button
        type="button"
        onClick={() => onSelectIndex(Math.min(versions.length - 1, clampedIndex + 1))}
        aria-label="Show next version"
        className="inline-flex h-6 w-6 items-center justify-center rounded-sm transition-colors hover:bg-accent/10 hover:text-foreground focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
      >
        <span aria-hidden>›</span>
      </button>
      <span className="sr-only" data-version-content>
        {versions[clampedIndex].content}
      </span>
    </div>
  );
}
