import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, FileJson, Loader2, RefreshCw, ShieldAlert, TriangleAlert, Wand2 } from "lucide-react";
import { toast } from "sonner";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ConfirmDialog, type ConfirmDialogState } from "@/components/documents/ConfirmDialog";
import { EmptyState } from "@/components/shared/EmptyState";

import { useNavigationGuardStore } from "@/stores/useNavigationGuardStore";
import { useCanvasCapabilities } from "@/hooks/useCanvasCapabilities";
import {
  CANVAS_DRAFT_RESTORED_NOTICE,
  CANVAS_EDIT_SELECTION_HINT,
  CANVAS_RESTORE_CONSEQUENCE,
  CANVAS_UNSAVED_CHANGES_WARNING,
} from "./labels";
import { CanvasCompare } from "./CanvasCompare";
import { CanvasPreview } from "./CanvasPreview";
import { CanvasVersionRail } from "./CanvasVersionRail";
import {
  canvasKeys,
  downloadCanvasVersion,
  editCanvasRange,
  exportCanvasManifest,
  getCanvasArtifact,
  getCanvasErrorDetail,
  getCanvasErrorStatus,
  getCanvasVersion,
  isCanvasVersionConflict,
  listCanvasVersions,
  restoreCanvasVersion,
  saveCanvasVersion,
  type CanvasVersion,
} from "@/lib/api/canvas";

const DRAFT_PERSIST_DEBOUNCE_MS = 500;

// ============================================================================
// localStorage draft persistence — every access wrapped in try/catch
// (DraftRoomDetailPage pattern). Persistence is best-effort: when storage is
// unavailable the in-memory draft still survives in-session navigation and
// the dirty banner still shows.
// ============================================================================

function canvasDraftKey(artifactUid: string): string {
  return `canvas-draft:${artifactUid}`;
}

function readCanvasDraft(artifactUid: string): string | null {
  try {
    return localStorage.getItem(canvasDraftKey(artifactUid));
  } catch {
    return null;
  }
}

function writeCanvasDraft(artifactUid: string, content: string): void {
  try {
    localStorage.setItem(canvasDraftKey(artifactUid), content);
  } catch {
    // Storage unavailable (private mode / quota) — best-effort only.
  }
}

function clearCanvasDraft(artifactUid: string): void {
  try {
    localStorage.removeItem(canvasDraftKey(artifactUid));
  } catch {
    // Ignore — nothing persisted means nothing to clear.
  }
}

/** Triggers a browser download of `blob` under `filename` without touching a
 * single byte of it, then releases the object URL (DraftExportDialog pattern). */
function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(url);
}

function manifestFilename(artifactName: string, versionNo: number): string {
  const slug = artifactName
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40);
  return `${slug || "canvas"}-v${versionNo}-manifest.json`;
}

/** 1-based inclusive line range covered by a textarea selection (LF-split). */
function selectionToLineRange(
  text: string,
  selectionStart: number,
  selectionEnd: number
): { startLine: number; endLine: number } | null {
  if (selectionEnd <= selectionStart) return null;
  const startLine = text.slice(0, selectionStart).split("\n").length;
  const endLine = text.slice(0, selectionEnd).split("\n").length;
  return { startLine, endLine };
}

function CanvasPageSkeleton() {
  return (
    <div className="space-y-4" data-testid="canvas-skeleton">
      <Skeleton className="h-6 w-40" />
      <Skeleton className="h-8 w-72" />
      <div className="flex flex-col gap-4 lg:flex-row">
        <Skeleton className="h-64 w-full lg:w-72" />
        <Skeleton className="h-64 flex-1" />
      </div>
    </div>
  );
}

type CanvasTab = "edit" | "preview" | "compare";

export default function CanvasPage() {
  const params = useParams<{ sessionId: string; artifactUid: string }>();
  const queryClient = useQueryClient();
  const artifactUid = params.artifactUid ?? "";
  const sessionId = params.sessionId ?? "";

  // Capability wiring (DraftRoomDetailPage pattern): the route renders even
  // when the feature is off so a direct visit gets an honest banner, never a
  // silent redirect. The backend reports disabled via 503 canvas_disabled or
  // {enabled:false}; both map to the banner below.
  const capabilitiesQuery = useCanvasCapabilities();
  const canvasDisabled =
    capabilitiesQuery.data?.enabled === false ||
    (capabilitiesQuery.isError && getCanvasErrorStatus(capabilitiesQuery.error) === 503);

  const artifactQuery = useQuery({
    queryKey: canvasKeys.artifact(artifactUid),
    queryFn: () => getCanvasArtifact(artifactUid),
    enabled: artifactUid !== "",
    retry: false,
  });

  const versionsQuery = useQuery({
    queryKey: canvasKeys.versions(artifactUid),
    queryFn: () => listCanvasVersions(artifactUid),
    enabled: artifactUid !== "" && artifactQuery.isSuccess,
    retry: false,
  });

  const artifact = artifactQuery.data?.artifact ?? null;
  const currentVersionNo = artifact?.current_version_no ?? null;
  const currentContent = artifactQuery.data?.version.content ?? null;

  // Editor working copy. `editorBaseVersionNo` records which server version
  // the copy was initialized from: the re-init effect only fires when the
  // server version is AHEAD of the editor (or the editor is uninitialized),
  // so a just-saved local copy is never clobbered by the still-stale query
  // cache during refetch.
  const [editorText, setEditorText] = useState<string | null>(null);
  const [editorBaseVersionNo, setEditorBaseVersionNo] = useState<number | null>(null);
  const [selectedVersionNo, setSelectedVersionNo] = useState<number | null>(null);
  const [versionName, setVersionName] = useState("");
  const [tab, setTab] = useState<CanvasTab>("edit");
  const [saving, setSaving] = useState(false);
  // Conflict banner mode: "save" offers force-save; "reload" (restore /
  // edit-range conflicts — no force escape hatch on those endpoints) only
  // offers reloading the latest version.
  const [conflict, setConflict] = useState<"save" | "reload" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [draftNotice, setDraftNotice] = useState<string | null>(null);
  const [restoreTarget, setRestoreTarget] = useState<number | null>(null);
  const [editRangeOpen, setEditRangeOpen] = useState(false);
  const [editRangeInstruction, setEditRangeInstruction] = useState("");
  const [editRangeTarget, setEditRangeTarget] = useState<{ startLine: number; endLine: number } | null>(null);
  const [selection, setSelection] = useState<{ start: number; end: number }>({ start: 0, end: 0 });
  // In-flight targeted-edit controller so "Stop" can abort a slow model call.
  const editRangeAbortRef = useRef<AbortController | null>(null);

  // Debounced-draft timer. Cancelled wherever the draft is discarded
  // (artifact switch / save / restore / reload) so a pending write can never
  // resurrect a draft after it was cleared.
  const draftTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const cancelDraftTimer = () => {
    if (draftTimerRef.current != null) {
      clearTimeout(draftTimerRef.current);
      draftTimerRef.current = null;
    }
  };

  // Reset all per-artifact state on navigation to a different artifact.
  useEffect(() => {
    cancelDraftTimer();
    setEditorText(null);
    setEditorBaseVersionNo(null);
    setSelectedVersionNo(null);
    setVersionName("");
    setTab("edit");
    setSaving(false);
    setConflict(null);
    setActionError(null);
    setDraftNotice(null);
    setRestoreTarget(null);
    setEditRangeOpen(false);
    setEditRangeInstruction("");
    setEditRangeTarget(null);
    setSelection({ start: 0, end: 0 });
  }, [artifactUid]);

  // Initialize (or re-initialize, when the server moved ahead) the editor
  // from the current version, rehydrating any persisted draft.
  useEffect(() => {
    if (currentContent == null || currentVersionNo == null) return;
    if (editorBaseVersionNo != null && currentVersionNo <= editorBaseVersionNo) return;
    const draft = readCanvasDraft(artifactUid);
    if (draft != null && draft !== currentContent) {
      setEditorText(draft);
      setDraftNotice(CANVAS_DRAFT_RESTORED_NOTICE);
    } else {
      setEditorText(currentContent);
      setDraftNotice(null);
    }
    setEditorBaseVersionNo(currentVersionNo);
    // Selection follows the current version on init and whenever the server
    // moved ahead of the editor base (e.g. after "Reload latest"); an older
    // selection is preserved only while it still makes sense.
    setSelectedVersionNo((prev) =>
      prev == null || currentVersionNo > prev ? currentVersionNo : prev
    );
  }, [artifactUid, currentContent, currentVersionNo, editorBaseVersionNo]);

  const isDirty = editorText != null && currentContent != null && editorText !== currentContent;

  // Debounced best-effort persistence of the unsaved draft.
  useEffect(() => {
    if (editorText == null || currentContent == null) return;
    if (editorText === currentContent) {
      clearCanvasDraft(artifactUid);
      return;
    }
    const timer = setTimeout(() => writeCanvasDraft(artifactUid, editorText), DRAFT_PERSIST_DEBOUNCE_MS);
    draftTimerRef.current = timer;
    return () => {
      clearTimeout(timer);
      if (draftTimerRef.current === timer) draftTimerRef.current = null;
    };
  }, [artifactUid, editorText, currentContent]);

  // Browser-level guard: refresh, tab close, or navigating away from the app
  // (DraftRoomDetailPage pattern).
  useEffect(() => {
    if (!isDirty) return;
    const handler = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [isDirty]);

  // In-app guard: capturing click listener for real `<a>` navigation plus the
  // shared store predicate consulted by App.tsx's programmatic navigation
  // (DraftRoomDetailPage pattern — plain BrowserRouter has no useBlocker).
  const setConfirmLeave = useNavigationGuardStore((s) => s.setConfirmLeave);
  useEffect(() => {
    if (!isDirty) {
      setConfirmLeave(null);
      return;
    }
    const confirmLeave = () => window.confirm(CANVAS_UNSAVED_CHANGES_WARNING);
    setConfirmLeave(confirmLeave);
    const handler = (event: MouseEvent) => {
      const target = (event.target as HTMLElement | null)?.closest("a[href]");
      if (!target) return;
      if (!confirmLeave()) {
        event.preventDefault();
        event.stopPropagation();
      }
    };
    document.addEventListener("click", handler, true);
    return () => {
      document.removeEventListener("click", handler, true);
      setConfirmLeave(null);
    };
  }, [isDirty, setConfirmLeave]);

  const viewingOldVersion =
    selectedVersionNo != null && currentVersionNo != null && selectedVersionNo !== currentVersionNo;

  const oldVersionQuery = useQuery({
    queryKey: canvasKeys.version(artifactUid, selectedVersionNo ?? -1),
    queryFn: () => getCanvasVersion(artifactUid, selectedVersionNo as number),
    enabled: viewingOldVersion,
    retry: false,
  });

  const activeContent = viewingOldVersion ? oldVersionQuery.data?.version.content ?? null : editorText;

  const versions = versionsQuery.data?.versions ?? [];

  // Mutation responses return the appended version directly (contract delta
  // #3) — no artifact envelope.
  const applyNewVersion = (version: CanvasVersion) => {
    cancelDraftTimer();
    clearCanvasDraft(artifactUid);
    setEditorText(version.content);
    setEditorBaseVersionNo(version.version_no);
    setSelectedVersionNo(version.version_no);
    setVersionName("");
    setConflict(null);
    setActionError(null);
  };

  const refreshArtifact = async () => {
    await queryClient.invalidateQueries({ queryKey: canvasKeys.artifact(artifactUid) });
  };

  const handleSave = async (force: boolean) => {
    if (editorText == null || currentVersionNo == null || saving) return;
    setSaving(true);
    setConflict(null);
    setActionError(null);
    try {
      const trimmedName = versionName.trim();
      const version = await saveCanvasVersion(artifactUid, {
        content: editorText,
        ...(trimmedName ? { name: trimmedName } : {}),
        base_version_no: currentVersionNo,
        force,
      });
      applyNewVersion(version);
      await refreshArtifact();
      toast.success(`Saved version ${version.version_no}`);
    } catch (err) {
      if (isCanvasVersionConflict(err)) {
        setConflict("save");
      } else {
        setActionError(getCanvasErrorDetail(err));
      }
    } finally {
      setSaving(false);
    }
  };

  const handleReloadLatest = async () => {
    cancelDraftTimer();
    clearCanvasDraft(artifactUid);
    setConflict(null);
    setDraftNotice(null);
    setEditorBaseVersionNo(null); // force re-init from the server on arrival
    setSelectedVersionNo(null); // follow the fresh current version, not the stale selection
    await artifactQuery.refetch();
    await versionsQuery.refetch();
  };

  const handleRestore = async (versionNo: number) => {
    if (currentVersionNo == null || saving) return;
    setSaving(true);
    setActionError(null);
    try {
      const version = await restoreCanvasVersion(artifactUid, {
        version_no: versionNo,
        base_version_no: currentVersionNo,
      });
      applyNewVersion(version);
      await refreshArtifact();
      toast.success(`Restored version ${versionNo} as version ${version.version_no}`);
    } catch (err) {
      if (isCanvasVersionConflict(err)) {
        // Restore has no force escape hatch — the only resolution is
        // reloading the latest version and retrying.
        setConflict("reload");
      } else {
        setActionError(getCanvasErrorDetail(err));
      }
    } finally {
      setSaving(false);
    }
  };

  const openEditRangeDialog = () => {
    if (editorText == null) return;
    const range = selectionToLineRange(editorText, selection.start, selection.end);
    if (!range) return;
    setEditRangeTarget(range);
    setEditRangeInstruction("");
    setEditRangeOpen(true);
  };

  const handleEditRange = async () => {
    if (currentVersionNo == null || editRangeTarget == null || saving) return;
    const instruction = editRangeInstruction.trim();
    if (!instruction) return;
    const controller = new AbortController();
    editRangeAbortRef.current = controller;
    // Cancellation shape: axios CanceledError (code ERR_CANCELED) or a DOM
    // AbortError — neither is a canvas failure, so the dialog stays open.
    const cancelled = (err: unknown): boolean => {
      const e = err as { code?: string; name?: string };
      return e?.code === "ERR_CANCELED" || e?.name === "CanceledError" || e?.name === "AbortError";
    };
    setSaving(true);
    setActionError(null);
    try {
      const version = await editCanvasRange(
        artifactUid,
        {
          start_line: editRangeTarget.startLine,
          end_line: editRangeTarget.endLine,
          instruction,
          base_version_no: currentVersionNo,
        },
        controller.signal
      );
      applyNewVersion(version);
      setEditRangeOpen(false);
      await refreshArtifact();
      toast.success(`Model edit saved as version ${version.version_no}`);
    } catch (err) {
      if (cancelled(err)) {
        // User stopped the edit: keep the dialog and instruction for retry.
        return;
      }
      if (isCanvasVersionConflict(err)) {
        setConflict("reload");
        setEditRangeOpen(false);
      } else {
        // 422 canvas_invalid_range / 502 canvas_model_unavailable surface the
        // backend detail verbatim; the dialog stays open for correction.
        setActionError(getCanvasErrorDetail(err));
      }
    } finally {
      editRangeAbortRef.current = null;
      setSaving(false);
    }
  };

  const handleStopEditRange = () => {
    editRangeAbortRef.current?.abort();
  };

  const handleDownloadSelected = async () => {
    if (selectedVersionNo == null) return;
    setActionError(null);
    try {
      const { blob, filename } = await downloadCanvasVersion(artifactUid, selectedVersionNo);
      downloadBlob(blob, filename);
      toast.success(`Downloaded ${filename}`);
    } catch (err) {
      setActionError(getCanvasErrorDetail(err));
    }
  };

  const handleExportManifest = async () => {
    if (selectedVersionNo == null || artifact == null) return;
    setActionError(null);
    try {
      const manifest = await exportCanvasManifest(artifactUid, selectedVersionNo);
      const json = JSON.stringify(manifest, null, 2);
      downloadBlob(
        new Blob([json], { type: "application/json" }),
        manifestFilename(artifact.name, selectedVersionNo)
      );
      toast.success("Exported manifest");
    } catch (err) {
      setActionError(getCanvasErrorDetail(err));
    }
  };

  // ============================================================================
  // Route states
  // ============================================================================

  if (artifactUid === "") {
    return (
      <EmptyState
        icon={TriangleAlert}
        title="This canvas could not be found"
        description="The link is invalid."
        action={<Link to={`/chat/${sessionId}`}>Back to chat</Link>}
      />
    );
  }

  if (artifactQuery.isLoading) {
    return <CanvasPageSkeleton />;
  }

  if (artifactQuery.isError) {
    const status = getCanvasErrorStatus(artifactQuery.error);
    if (status === 404) {
      return (
        <EmptyState
          icon={TriangleAlert}
          title="This canvas could not be found"
          description="It may have been deleted, or the link is no longer valid."
          action={<Link to={`/chat/${sessionId}`}>Back to chat</Link>}
        />
      );
    }
    if (status === 403) {
      return (
        <EmptyState
          icon={ShieldAlert}
          title="You don't have access to this canvas"
          description="Ask an admin for access to this session's vault."
          action={<Link to={`/chat/${sessionId}`}>Back to chat</Link>}
        />
      );
    }
    return (
      <EmptyState
        icon={TriangleAlert}
        title="Could not load this canvas"
        description={getCanvasErrorDetail(artifactQuery.error)}
        action={{ label: "Retry", onClick: () => void artifactQuery.refetch() }}
      />
    );
  }

  if (!artifact || currentVersionNo == null || editorText == null) {
    return <CanvasPageSkeleton />;
  }

  const hasSelection = !viewingOldVersion && selection.end > selection.start;
  const canEditSelection = hasSelection && !isDirty && !saving;
  const restoreDialogState: ConfirmDialogState | null =
    restoreTarget != null
      ? {
          open: true,
          title: `Restore version ${restoreTarget}?`,
          description: CANVAS_RESTORE_CONSEQUENCE,
          onConfirm: () => {
            const target = restoreTarget;
            setRestoreTarget(null);
            if (target != null) void handleRestore(target);
          },
        }
      : null;

  return (
    <section
      className="animate-in fade-in space-y-4 pb-12 duration-300"
      aria-labelledby="canvas-page-heading"
    >
      <div aria-live="polite" className="sr-only" data-testid="canvas-live-notices">
        {draftNotice}
      </div>

      {canvasDisabled && (
        <Alert variant="warning" data-testid="canvas-disabled-banner">
          <AlertDescription>
            Canvas is disabled on this server. Saved versions remain readable, but creating new
            versions is unavailable until an operator enables it.
          </AlertDescription>
        </Alert>
      )}

      <div>
        <Link
          to={`/chat/${sessionId}`}
          className="text-sm text-muted-foreground hover:underline"
          aria-label="Back to chat"
        >
          &larr; Back to chat
        </Link>
        <div className="flex flex-wrap items-center gap-2">
          <h1 id="canvas-page-heading" className="text-2xl font-semibold tracking-tight">
            {artifact.name}
          </h1>
          <Badge variant="outline">{artifact.kind}</Badge>
          {artifact.language && (
            <Badge variant="secondary">{artifact.language}</Badge>
          )}
        </div>
      </div>

      {conflict && (
        <Alert variant="destructive" data-testid="canvas-conflict-banner" role="alert">
          <AlertTitle>Version conflict</AlertTitle>
          <AlertDescription>
            <p className="mb-2">
              {conflict === "save"
                ? "This canvas was saved by someone else while you were editing. Reload the latest version, or save anyway to append your changes as a new version."
                : "This canvas changed while you were working, so the action was rejected to keep history safe. Reload the latest version and try again."}
            </p>
            <div className="flex flex-wrap gap-2">
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => void handleReloadLatest()}
                disabled={saving}
                aria-label="Reload latest version"
              >
                <RefreshCw className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                Reload latest
              </Button>
              {conflict === "save" && (
                <Button
                  type="button"
                  size="sm"
                  onClick={() => void handleSave(true)}
                  disabled={saving}
                  aria-label="Save anyway as a new version"
                  data-testid="canvas-conflict-save-anyway"
                >
                  Save anyway
                </Button>
              )}
            </div>
          </AlertDescription>
        </Alert>
      )}

      {actionError && (
        <Alert variant="destructive" role="alert" data-testid="canvas-action-error">
          <AlertDescription>{actionError}</AlertDescription>
        </Alert>
      )}

      {draftNotice && (
        <Alert data-testid="canvas-draft-restored-notice">
          <AlertDescription>{draftNotice}</AlertDescription>
        </Alert>
      )}

      {/* Responsive: the version rail stacks above the editor on narrow
          viewports and sits alongside it from lg up. */}
      <div className="flex flex-col gap-4 lg:flex-row" data-testid="canvas-layout">
        <div className="flex shrink-0 flex-col gap-2 lg:w-72" data-testid="canvas-version-rail">
          <h2 className="text-sm font-semibold text-muted-foreground">Versions</h2>
          {versionsQuery.isLoading ? (
            <div className="space-y-2" data-testid="canvas-versions-skeleton">
              <Skeleton className="h-14 w-full" />
              <Skeleton className="h-14 w-full" />
            </div>
          ) : versionsQuery.isError ? (
            <Alert variant="destructive">
              <AlertDescription>
                Couldn't load versions.
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="ml-2"
                  onClick={() => void versionsQuery.refetch()}
                  aria-label="Retry loading versions"
                >
                  Retry
                </Button>
              </AlertDescription>
            </Alert>
          ) : (
            <CanvasVersionRail
              versions={versions}
              selectedVersionNo={selectedVersionNo}
              currentVersionNo={currentVersionNo}
              onSelect={setSelectedVersionNo}
            />
          )}
        </div>

        <div className="min-w-0 flex-1 space-y-3">
          {viewingOldVersion && (
            <Alert>
              <AlertDescription data-testid="canvas-readonly-notice">
                Viewing version {selectedVersionNo} (read-only). Select the current version
                ({currentVersionNo}) to edit.
              </AlertDescription>
            </Alert>
          )}

          <Tabs value={tab} onValueChange={(next) => setTab(next as CanvasTab)}>
            <TabsList aria-label="Canvas view tabs">
              <TabsTrigger value="edit" aria-label="Edit tab">
                Edit
              </TabsTrigger>
              <TabsTrigger value="preview" aria-label="Preview tab">
                Preview
              </TabsTrigger>
              <TabsTrigger value="compare" aria-label="Compare tab">
                Compare
              </TabsTrigger>
            </TabsList>

            <TabsContent value="edit" className="space-y-3">
              <div className="flex flex-wrap items-end gap-2">
                <div className="min-w-48 flex-1">
                  <Label htmlFor="canvas-version-name">Version name (optional)</Label>
                  <Input
                    id="canvas-version-name"
                    aria-label="Version name"
                    placeholder="e.g. After refactor"
                    value={versionName}
                    onChange={(event) => setVersionName(event.target.value)}
                    disabled={saving || viewingOldVersion}
                  />
                </div>
                <Button
                  type="button"
                  onClick={() => void handleSave(false)}
                  disabled={saving || viewingOldVersion || !isDirty}
                  aria-label="Save as a new version"
                  data-testid="canvas-save-button"
                >
                  {saving ? (
                    <>
                      <Loader2 className="mr-1.5 h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden />
                      Saving…
                    </>
                  ) : (
                    "Save version"
                  )}
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  onClick={openEditRangeDialog}
                  disabled={!canEditSelection}
                  aria-label="Edit selection with model"
                  title={isDirty && hasSelection ? CANVAS_EDIT_SELECTION_HINT : undefined}
                  data-testid="canvas-edit-selection-button"
                >
                  <Wand2 className="mr-1.5 h-4 w-4" aria-hidden />
                  Edit selection with model
                </Button>
              </div>

              <div className="flex items-center justify-between gap-2">
                <p className="text-xs text-muted-foreground">
                  {isDirty ? (
                    <span className="font-medium text-warning" data-testid="canvas-dirty-indicator">
                      Unsaved changes
                    </span>
                  ) : (
                    <span>All changes saved (version {currentVersionNo})</span>
                  )}
                </p>
                <p className="text-xs text-muted-foreground">
                  {CANVAS_EDIT_SELECTION_HINT}
                </p>
              </div>

              {viewingOldVersion && oldVersionQuery.isLoading ? (
                <Skeleton className="h-64 w-full" data-testid="canvas-old-version-skeleton" />
              ) : (
                <Textarea
                  id={`canvas-editor-${artifactUid}`}
                  aria-label="Canvas content editor"
                  value={activeContent ?? ""}
                  onChange={(event) => setEditorText(event.target.value)}
                  onSelect={(event) =>
                    setSelection({
                      start: event.currentTarget.selectionStart,
                      end: event.currentTarget.selectionEnd,
                    })
                  }
                  readOnly={viewingOldVersion}
                  aria-readonly={viewingOldVersion}
                  disabled={saving}
                  className="min-h-[420px] font-mono text-sm leading-relaxed"
                  data-testid="canvas-editor"
                />
              )}
            </TabsContent>

            <TabsContent value="preview">
              {activeContent == null ? (
                <Skeleton className="h-64 w-full" />
              ) : (
                <CanvasPreview
                  content={activeContent}
                  kind={artifact.kind}
                  language={artifact.language}
                />
              )}
            </TabsContent>

            <TabsContent value="compare">
              <CanvasCompare artifactUid={artifactUid} versions={versions} />
            </TabsContent>
          </Tabs>

          <div className="flex flex-wrap gap-2 border-t border-border pt-3">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => void handleDownloadSelected()}
              disabled={selectedVersionNo == null}
              aria-label={`Download version ${selectedVersionNo ?? ""}`}
              data-testid="canvas-download-button"
            >
              <Download className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              Download version {selectedVersionNo ?? ""}
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => void handleExportManifest()}
              disabled={selectedVersionNo == null}
              aria-label={`Export manifest for version ${selectedVersionNo ?? ""}`}
              data-testid="canvas-export-manifest-button"
            >
              <FileJson className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              Export manifest (JSON)
            </Button>
            {viewingOldVersion && selectedVersionNo != null && (
              <Button
                type="button"
                size="sm"
                onClick={() => setRestoreTarget(selectedVersionNo)}
                disabled={saving}
                aria-label={`Restore version ${selectedVersionNo}`}
                data-testid="canvas-restore-button"
              >
                Restore this version
              </Button>
            )}
          </div>
        </div>
      </div>

      {restoreDialogState && (
        <ConfirmDialog
          state={restoreDialogState}
          onOpenChange={(open) => {
            if (!open) setRestoreTarget(null);
          }}
        />
      )}

      <Dialog open={editRangeOpen} onOpenChange={setEditRangeOpen}>
        <DialogContent aria-describedby="canvas-edit-range-desc">
          <DialogHeader>
            <DialogTitle>Edit selection with model</DialogTitle>
            <DialogDescription id="canvas-edit-range-desc">
              {editRangeTarget
                ? `Lines ${editRangeTarget.startLine}–${editRangeTarget.endLine} will be replaced. Only your selection is changed.`
                : ""}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <Label htmlFor="canvas-edit-range-instruction">Instruction</Label>
            <Textarea
              id="canvas-edit-range-instruction"
              aria-label="Model edit instruction"
              placeholder="e.g. Add error handling to these lines"
              value={editRangeInstruction}
              onChange={(event) => setEditRangeInstruction(event.target.value)}
              className="min-h-24"
            />
          </div>
          {actionError && (
            <Alert variant="destructive" role="alert">
              <AlertDescription>{actionError}</AlertDescription>
            </Alert>
          )}
          <DialogFooter>
            {saving ? (
              <Button
                type="button"
                variant="outline"
                onClick={handleStopEditRange}
                aria-label="Stop model edit"
                data-testid="canvas-edit-range-stop"
              >
                Stop
              </Button>
            ) : (
              <Button type="button" variant="outline" onClick={() => setEditRangeOpen(false)}>
                Cancel
              </Button>
            )}
            <Button
              type="button"
              onClick={() => void handleEditRange()}
              disabled={saving || !editRangeInstruction.trim()}
              aria-label="Apply model edit"
              data-testid="canvas-edit-range-apply"
            >
              {saving ? (
                <>
                  <Loader2 className="mr-1.5 h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden />
                  Applying…
                </>
              ) : (
                "Apply edit"
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}
