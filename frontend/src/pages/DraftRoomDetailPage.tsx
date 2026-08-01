import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ShieldAlert, TriangleAlert } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/EmptyState";
import { Skeleton } from "@/components/ui/skeleton";

import { DraftStatusBanner } from "@/components/draft-room/DraftStatusBanner";
import { DraftWorkspace, type DraftWorkspaceHandle, type DraftDerivedStatus } from "@/components/draft-room/DraftWorkspace";
import { DRAFT_ROOM_DISABLED_MESSAGE } from "@/components/draft-room/labels";
import { useDraftRoomCapabilities } from "@/hooks/useDraftRoomCapabilities";
import { useDraftRoomEvents } from "@/hooks/useDraftRoomEvents";
import { useDraftRoomUiStore } from "@/stores/useDraftRoomUiStore";
import {
  draftRoomKeys,
  getDraft,
  getDraftRevision,
  parseDraftRoomError,
  type DraftFactStatus,
} from "@/lib/api/draftRoom";

/**
 * Not part of the shared `labels.ts` (this worker doesn't own that file) —
 * a page-local confirmation string for the one route-transition guard this
 * page owns.
 */
const UNSAVED_CHANGES_WARNING =
  "You have unsaved changes to this draft. Leaving now will discard them. Continue?";

function DraftRoomDetailSkeleton() {
  return (
    <div className="space-y-4" data-testid="draft-detail-skeleton">
      <Skeleton className="h-8 w-64" />
      <Skeleton className="h-10 w-full" />
      <Skeleton className="h-64 w-full" />
    </div>
  );
}

export default function DraftRoomDetailPage() {
  const params = useParams<{ draftId: string }>();
  const navigate = useNavigate();
  const draftIdNum = Number(params.draftId);
  const isValidId = params.draftId != null && Number.isInteger(draftIdNum) && draftIdNum > 0;

  const capabilitiesQuery = useDraftRoomCapabilities();

  const detailQuery = useQuery({
    queryKey: draftRoomKeys.detail(draftIdNum),
    queryFn: () => getDraft(draftIdNum),
    enabled: isValidId,
    retry: false,
  });

  const { pollingFallback } = useDraftRoomEvents(isValidId ? draftIdNum : null, {
    enabled: isValidId && detailQuery.isSuccess,
  });

  // Announce the polling-fallback state politely, but only on the transition
  // into or out of it — never on every render — matching DraftEditor's
  // dirty-state announcement pattern.
  const [pollingAnnouncement, setPollingAnnouncement] = useState("");
  const previousPollingFallbackRef = useRef(pollingFallback);
  useEffect(() => {
    if (previousPollingFallbackRef.current !== pollingFallback) {
      previousPollingFallbackRef.current = pollingFallback;
      setPollingAnnouncement(
        pollingFallback
          ? "Live updates are unavailable. Checking for changes periodically."
          : "Live updates restored."
      );
    }
  }, [pollingFallback]);

  const resetForDraft = useDraftRoomUiStore((s) => s.resetForDraft);
  const storedDraftText = useDraftRoomUiStore((s) =>
    isValidId ? s.draftText[draftIdNum] : undefined
  );

  useEffect(() => {
    if (isValidId) resetForDraft(draftIdNum);
    // Reset the view-selection state only on navigation to a (new) draft id.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draftIdNum, isValidId]);

  const currentRevisionId = detailQuery.data?.current_revision_summary?.id ?? null;
  const currentRevisionQuery = useQuery({
    queryKey: draftRoomKeys.revision(draftIdNum, currentRevisionId ?? -1),
    queryFn: () => getDraftRevision(draftIdNum, currentRevisionId as number),
    enabled: isValidId && currentRevisionId != null,
  });
  const baselineContent = currentRevisionQuery.data?.content_md ?? "";
  const isDirty = storedDraftText != null && storedDraftText !== baselineContent;

  // Browser-level guard: refresh, tab close, or navigating away from the app.
  useEffect(() => {
    if (!isDirty) return;
    const handler = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [isDirty]);

  // In-app guard: this app uses a plain `<BrowserRouter>` (no data router), so
  // `useBlocker`/`unstable_usePrompt` aren't available. Every in-app
  // navigation in this codebase renders as a real `<a>` (react-router's
  // `Link`/`NavLink`), so a capturing click listener on the nearest anchor
  // covers the nav rail, breadcrumbs, and any link this page itself renders,
  // without needing cooperation from the components that own those links.
  useEffect(() => {
    if (!isDirty) return;
    const handler = (event: MouseEvent) => {
      const target = (event.target as HTMLElement | null)?.closest("a[href]");
      if (!target) return;
      if (!window.confirm(UNSAVED_CHANGES_WARNING)) {
        event.preventDefault();
        event.stopPropagation();
      }
    };
    document.addEventListener("click", handler, true);
    return () => document.removeEventListener("click", handler, true);
  }, [isDirty]);

  const [derivedStatus, setDerivedStatus] = useState<DraftDerivedStatus>({
    sourceOnly: false,
    retrievalPartial: false,
    evidenceInvalidated: false,
  });
  const handleDerivedStatus = useCallback((next: DraftDerivedStatus) => {
    setDerivedStatus((prev) =>
      prev.sourceOnly === next.sourceOnly &&
      prev.retrievalPartial === next.retrievalPartial &&
      prev.evidenceInvalidated === next.evidenceInvalidated
        ? prev
        : next
    );
  }, []);

  const workspaceRef = useRef<DraftWorkspaceHandle>(null);
  const handleRerunResearch = useCallback(() => workspaceRef.current?.requestCompile("research"), []);
  const handleRerunNewsroom = useCallback(() => workspaceRef.current?.requestCompile(undefined), []);

  if (!isValidId) {
    return (
      <EmptyState
        icon={TriangleAlert}
        title="Draft not found"
        description="This project doesn't exist or the link is invalid."
        action={{ label: "Back to Draft Room", onClick: () => navigate("/draft-room") }}
      />
    );
  }

  if (detailQuery.isLoading || capabilitiesQuery.isLoading) {
    return <DraftRoomDetailSkeleton />;
  }

  if (detailQuery.isError) {
    const info = parseDraftRoomError(detailQuery.error);
    if (info.status === 404) {
      return (
        <EmptyState
          icon={TriangleAlert}
          title="Draft not found"
          description="This project doesn't exist, or it was deleted."
          action={{ label: "Back to Draft Room", onClick: () => navigate("/draft-room") }}
        />
      );
    }
    if (info.status === 403) {
      return (
        <EmptyState
          icon={ShieldAlert}
          title="You don't have access to this project"
          description="Ask an admin for access to this project's vault, or go back to your own projects."
          action={{ label: "Back to Draft Room", onClick: () => navigate("/draft-room") }}
        />
      );
    }
    return (
      <EmptyState
        icon={TriangleAlert}
        title="Could not load this project"
        description={info.detail}
        action={{ label: "Back to Draft Room", onClick: () => navigate("/draft-room") }}
      />
    );
  }

  const detail = detailQuery.data;
  if (!detail) return <DraftRoomDetailSkeleton />;

  const draft = detail.summary;
  const capabilityDisabled = !capabilitiesQuery.isLoading && capabilitiesQuery.data?.enabled === false;

  return (
    <section className="animate-in fade-in space-y-4 pb-12 duration-300" aria-labelledby="draft-room-detail-heading">
      <div aria-live="polite" className="sr-only">
        {pollingAnnouncement}
      </div>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <Link to="/draft-room" className="text-sm text-muted-foreground hover:underline">
            &larr; Back to Draft Room
          </Link>
          <h1 id="draft-room-detail-heading" className="text-2xl font-semibold tracking-tight">
            {draft.title}
          </h1>
        </div>
        {pollingFallback && (
          <Badge
            variant="outline"
            title="Live updates are unavailable. This page is polling for changes instead."
          >
            Polling for updates
          </Badge>
        )}
      </div>

      {capabilityDisabled && (
        <Alert variant="warning">
          <AlertDescription>{DRAFT_ROOM_DISABLED_MESSAGE}</AlertDescription>
        </Alert>
      )}

      <DraftStatusBanner
        draft={draft}
        detail={detail}
        factStatus={(detail.current_revision_summary?.fact_status ?? null) as DraftFactStatus | null}
        sourceOnly={derivedStatus.sourceOnly}
        retrievalPartial={derivedStatus.retrievalPartial}
        evidenceInvalidated={derivedStatus.evidenceInvalidated}
        vaultAccess={draft.vault_access}
        onRerunResearch={handleRerunResearch}
        onRerunNewsroom={handleRerunNewsroom}
      />

      <DraftWorkspace
        ref={workspaceRef}
        draftId={draftIdNum}
        draft={draft}
        detail={detail}
        capabilities={capabilitiesQuery.data}
        vaultAccess={draft.vault_access}
        onDerivedStatus={handleDerivedStatus}
      />
    </section>
  );
}
