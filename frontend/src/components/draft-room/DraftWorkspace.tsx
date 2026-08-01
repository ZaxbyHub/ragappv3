import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { Loader2, RotateCcw } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

import {
  DRAFT_ASSIGNMENT_FIELD_ORDER,
  DraftAssignmentForm,
  focusFirstInvalidDraftAssignmentField,
  validateDraftAssignmentForm,
  type DraftAssignmentFormValue,
} from "./DraftAssignmentForm";
import { DraftSourceUpload } from "./DraftSourceUpload";
import { DraftSourceList } from "./DraftSourceList";
import { DraftEvidencePanel } from "./DraftEvidencePanel";
import { DraftEditor } from "./DraftEditor";
import { DraftPreview } from "./DraftPreview";
import { DraftRevisionDiff } from "./DraftRevisionDiff";
import { DraftStageArtifact } from "./DraftStageArtifact";
import { DraftStageRail } from "./DraftStageRail";
import { DraftInspector } from "./DraftInspector";
import { DraftReadyDialog, type ReadyEligibility } from "./DraftReadyDialog";
import { DraftExportDialog } from "./DraftExportDialog";
import { DraftPromoteDialog } from "./DraftPromoteDialog";
import {
  CANCEL_CONSEQUENCE,
  DRAFT_ROOM_DISABLED_MESSAGE,
  EXPORT_CTA,
  INPUT_ROLE_LABELS,
  MARK_READY_CTA,
  MODE_LABELS,
  PROMOTE_TO_VAULT_CTA,
  PROVIDER_DISCLOSURE,
  READY_BLOCKER_LABELS,
  SAVE_REVISION_CONSEQUENCE,
  SAVE_REVISION_CTA,
  STAGE_LABELS,
  TIER_LABELS,
  compileCtaLabel,
} from "./labels";
import {
  BLOCKING_CLAIM_STATUSES,
  FACT_CURRENT_STATUSES,
  archiveDraft,
  cancelDraftJob,
  compileDraft,
  createDraftRevision,
  deleteDraft,
  draftRoomKeys,
  getDraftRevision,
  getDraftStages,
  listDraftFindings,
  listDraftJobs,
  listDraftRevisions,
  parseDraftRoomError,
  restoreDraft,
  retryDraftJob,
  updateDraft,
  type CompileRequest,
  type DraftCompileStartStage,
  type DraftDetail,
  type DraftInput,
  type DraftJob,
  type DraftRoomCapabilities,
  type DraftStage,
  type DraftStageName,
  type DraftSummary,
  type DraftUpdateRequest,
  type DraftVaultAccess,
  type PromoteResponse,
} from "@/lib/api/draftRoom";
import { useDraftRoomUiStore, type EditorTab, type WorkspaceTab } from "@/stores/useDraftRoomUiStore";

// ---------------------------------------------------------------------------
// Bounded, best-effort lookups (mirrors the pattern in DraftClaimsPanel.tsx —
// small, single-page fetches used only to enrich a secondary display, never
// the source of truth the server itself remains authoritative for).
// ---------------------------------------------------------------------------
const JOBS_LOOKUP_PAGE_SIZE = 10;
const STAGES_LOOKUP_PAGE_SIZE = 100;
const REVISIONS_LOOKUP_PAGE_SIZE = 50;
const OPEN_FINDINGS_LOOKUP_PAGE_SIZE = 100;

const FACT_CURRENT = new Set<string>(FACT_CURRENT_STATUSES);

const WORKSPACE_TAB_ITEMS: ReadonlyArray<{ value: WorkspaceTab; label: string }> = [
  { value: "assignment", label: "Assignment" },
  { value: "sources", label: "Sources" },
  { value: "research", label: STAGE_LABELS.research },
  { value: "outline", label: STAGE_LABELS.outline },
  { value: "draft", label: STAGE_LABELS.draft },
  { value: "copy", label: STAGE_LABELS.copy },
  { value: "standards", label: STAGE_LABELS.standards },
  { value: "fact", label: STAGE_LABELS.fact },
];

const STAGE_VIEW_TABS = new Set<WorkspaceTab>(["research", "outline", "copy", "standards", "fact"]);

function findLatestStage(stages: DraftStage[], name: string | null): DraftStage | null {
  if (!name) return null;
  let latest: DraftStage | null = null;
  for (const entry of stages) {
    if (entry.stage !== name) continue;
    if (!latest || entry.attempt > latest.attempt) latest = entry;
  }
  return latest;
}

/**
 * Reads the research stage's `source_only` / `retrieval_status` fields
 * defensively — the artifact is `unknown` on the wire (see
 * DraftStageArtifact.tsx's own defensive readers) — without claiming
 * anything about a stage that never completed.
 */
function readResearchFlags(researchStage: DraftStage | null): { sourceOnly: boolean; retrievalPartial: boolean } {
  if (!researchStage || researchStage.status !== "completed") {
    return { sourceOnly: false, retrievalPartial: false };
  }
  const artifact = researchStage.artifact;
  if (typeof artifact !== "object" || artifact === null) return { sourceOnly: false, retrievalPartial: false };
  const record = artifact as Record<string, unknown>;
  return { sourceOnly: record.source_only === true, retrievalPartial: record.retrieval_status === "partial" };
}

function toFormValue(draft: DraftSummary, brief: DraftDetail["brief"]): DraftAssignmentFormValue {
  return { title: draft.title, vault_id: draft.vault_id, mode: draft.mode, tier: draft.tier, brief };
}

function summarizeRoles(inputs: DraftInput[]): string {
  if (inputs.length === 0) return "No source files";
  const counts = new Map<string, number>();
  for (const input of inputs) counts.set(input.role, (counts.get(input.role) ?? 0) + 1);
  return Array.from(counts.entries())
    .map(([role, count]) => `${count} ${INPUT_ROLE_LABELS[role] ?? role}`)
    .join(", ");
}

/**
 * "Actor/time" for the Ready state (SPEC row: Ready). `draft` here always
 * comes from `getDraft()` (the detail endpoint), which resolves
 * `ready_by_username` whenever `ready_by` is set — a `null` username with a
 * present `ready_by` on *this* endpoint specifically means the approver's
 * account was later deleted, never "unresolved". Never renders the bare
 * numeric id as if it were a person's name. Returns `null` for a draft that
 * has never been marked Ready.
 */
function readyApprovalText(draft: DraftSummary): string | null {
  if (draft.status !== "ready") return null;
  const when = draft.ready_at ? new Date(draft.ready_at).toLocaleString() : null;
  const suffix = when ? ` on ${when}` : "";
  if (draft.ready_by_username) return `Approved by ${draft.ready_by_username}${suffix}`;
  if (draft.ready_by != null) return `Approved by a user who no longer has an account${suffix}`;
  return `Approved${suffix}`;
}

function generateIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `draft-compile-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export interface DraftDerivedStatus {
  sourceOnly: boolean;
  retrievalPartial: boolean;
  evidenceInvalidated: boolean;
}

export interface DraftWorkspaceProps {
  draftId: number;
  draft: DraftSummary;
  detail: DraftDetail;
  /** Absent while capabilities are still loading; every gated action fails closed until present. */
  capabilities: DraftRoomCapabilities | undefined;
  vaultAccess: DraftVaultAccess;
  /**
   * Reports the source-only / retrieval-partial / evidence-invalidated flags
   * this component already derives from its own stage/revision queries, so
   * the caller (DraftRoomDetailPage) can feed the same values into
   * `DraftStatusBanner` without independently re-fetching or re-deriving
   * them.
   */
  onDerivedStatus?(status: DraftDerivedStatus): void;
}

export interface DraftWorkspaceHandle {
  /** Opens the compile confirmation dialog, optionally pinned to a start stage. */
  requestCompile(startStage?: DraftCompileStartStage): void;
}

/**
 * Composes the stage rail, project controls (assignment/sources/stage
 * shortcuts), the manuscript editor/preview/compare area, and the inspector.
 * Owns the compile / cancel / retry / save-revision mutations and the Ready /
 * Export / Promote dialog triggers.
 */
export const DraftWorkspace = forwardRef<DraftWorkspaceHandle, DraftWorkspaceProps>(function DraftWorkspace(
  { draftId, draft, detail, capabilities, vaultAccess, onDerivedStatus },
  ref
) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  const workspaceTab = useDraftRoomUiStore((s) => s.workspaceTab);
  const setWorkspaceTab = useDraftRoomUiStore((s) => s.setWorkspaceTab);
  const editorTab = useDraftRoomUiStore((s) => s.editorTab);
  const setEditorTab = useDraftRoomUiStore((s) => s.setEditorTab);
  const selectedStage = useDraftRoomUiStore((s) => s.selectedStage);
  const setSelectedStage = useDraftRoomUiStore((s) => s.setSelectedStage);
  const setInspectorTab = useDraftRoomUiStore((s) => s.setInspectorTab);
  const compareFromRevisionId = useDraftRoomUiStore((s) => s.compareFromRevisionId);
  const compareToRevisionId = useDraftRoomUiStore((s) => s.compareToRevisionId);
  const setCompareRevisions = useDraftRoomUiStore((s) => s.setCompareRevisions);
  const sourcesSheetOpen = useDraftRoomUiStore((s) => s.sourcesSheetOpen);
  const setSourcesSheetOpen = useDraftRoomUiStore((s) => s.setSourcesSheetOpen);
  const storedDraftText = useDraftRoomUiStore((s) => s.draftText[draftId]);
  const setDraftTextStore = useDraftRoomUiStore((s) => s.setDraftText);
  const clearDraftTextStore = useDraftRoomUiStore((s) => s.clearDraftText);

  const [evidenceSheetOpen, setEvidenceSheetOpen] = useState(false);

  // ---- Permission model ---------------------------------------------------
  // Two tiers, documented here since the API doesn't expose a single flag:
  //  - `canManageContent`: draft-private actions (sources, brief, saving a
  //    revision, disposing findings) that never touch the vault itself.
  //    Blocked only by archival or a fully revoked vault relationship — per
  //    VAULT_ACCESS_REVOKED_WARNING's own text, revoked access leaves only
  //    "cancel runs and delete the project" available.
  //  - `canCompileOrPromote`: actions that read from or write into the vault
  //    (compiling reads vault evidence; promoting writes a new document),
  //    which require `write` vault access specifically, not just `read`.
  const archived = draft.status === "archived";
  const vaultRevoked = vaultAccess === "revoked";
  const activeJob = detail.active_compile_job;
  const hasActiveJob = activeJob != null;
  const canManageContent = !archived && !vaultRevoked;
  const canCompileOrPromote = canManageContent && vaultAccess === "write";

  // ---- Job / stage lookups -------------------------------------------------
  const jobsQuery = useQuery({
    queryKey: draftRoomKeys.jobs(draftId, { per_page: JOBS_LOOKUP_PAGE_SIZE }),
    queryFn: () => listDraftJobs(draftId, { per_page: JOBS_LOOKUP_PAGE_SIZE }),
  });

  const latestCompileJob = useMemo<DraftJob | null>(() => {
    const jobs = (jobsQuery.data?.items ?? []).filter((job) => job.job_type === "compile");
    if (jobs.length === 0) return null;
    return jobs.reduce((latest, job) => (job.created_at > latest.created_at ? job : latest));
  }, [jobsQuery.data]);

  const relevantJobId = activeJob?.id ?? detail.current_revision_summary?.job_id ?? latestCompileJob?.id ?? null;
  const relevantJobStatus = activeJob?.status ?? latestCompileJob?.status ?? null;
  const relevantJobActiveStage = activeJob?.active_stage ?? null;

  const stagesQuery = useQuery({
    queryKey: draftRoomKeys.stages(draftId, relevantJobId ?? -1, false),
    queryFn: () => getDraftStages(draftId, relevantJobId as number, { per_page: STAGES_LOOKUP_PAGE_SIZE }),
    enabled: relevantJobId != null,
  });
  const stages = useMemo(() => stagesQuery.data?.items ?? [], [stagesQuery.data]);

  const revisionsQuery = useQuery({
    queryKey: draftRoomKeys.revisions(draftId, { per_page: REVISIONS_LOOKUP_PAGE_SIZE }),
    queryFn: () => listDraftRevisions(draftId, { per_page: REVISIONS_LOOKUP_PAGE_SIZE }),
  });
  const revisions = revisionsQuery.data?.items ?? [];
  const currentRevisionId = detail.current_revision_summary?.id ?? null;

  const currentRevisionDetailQuery = useQuery({
    queryKey: draftRoomKeys.revision(draftId, currentRevisionId ?? -1),
    queryFn: () => getDraftRevision(draftId, currentRevisionId as number),
    enabled: currentRevisionId != null,
  });
  const baselineContent = currentRevisionDetailQuery.data?.content_md ?? "";
  const editorValue = storedDraftText ?? baselineContent;
  const isDirty = storedDraftText != null && storedDraftText !== baselineContent;

  const compareFromQuery = useQuery({
    queryKey: draftRoomKeys.revision(draftId, compareFromRevisionId ?? -1),
    queryFn: () => getDraftRevision(draftId, compareFromRevisionId as number),
    enabled: compareFromRevisionId != null,
  });
  const compareToQuery = useQuery({
    queryKey: draftRoomKeys.revision(draftId, compareToRevisionId ?? -1),
    queryFn: () => getDraftRevision(draftId, compareToRevisionId as number),
    enabled: compareToRevisionId != null,
  });

  // Best-effort per-stage blocker/warning counts for the stage rail, from a
  // single bounded page of open findings for the current revision.
  const openFindingsQuery = useQuery({
    queryKey: [
      ...draftRoomKeys.findings(draftId, {
        status: "open",
        revision_id: currentRevisionId,
        per_page: OPEN_FINDINGS_LOOKUP_PAGE_SIZE,
      }),
      "stage-rail-counts",
    ] as const,
    queryFn: () =>
      listDraftFindings(draftId, {
        status: "open",
        revision_id: currentRevisionId ?? undefined,
        per_page: OPEN_FINDINGS_LOOKUP_PAGE_SIZE,
      }),
    enabled: currentRevisionId != null,
  });
  const { blockerCountsByStage, warningCountsByStage } = useMemo(() => {
    const blockers: Record<string, number> = {};
    const warnings: Record<string, number> = {};
    for (const finding of openFindingsQuery.data?.items ?? []) {
      if (finding.severity === "blocker") blockers[finding.stage] = (blockers[finding.stage] ?? 0) + 1;
      else if (finding.severity === "warning") warnings[finding.stage] = (warnings[finding.stage] ?? 0) + 1;
    }
    return { blockerCountsByStage: blockers, warningCountsByStage: warnings };
  }, [openFindingsQuery.data]);

  function handleSelectStage(stageName: string) {
    setSelectedStage(stageName);
    setInspectorTab("artifact");
  }
  const selectedStageEntry = findLatestStage(stages, selectedStage);
  const inspectorJobId = relevantJobId;

  // ---- Status derived for the caller's DraftStatusBanner -------------------
  const { sourceOnly, retrievalPartial } = readResearchFlags(findLatestStage(stages, "research"));
  const evidenceInvalidated = detail.current_revision_summary?.fact_status === "invalidated";

  useEffect(() => {
    onDerivedStatus?.({ sourceOnly, retrievalPartial, evidenceInvalidated });
  }, [sourceOnly, retrievalPartial, evidenceInvalidated, onDerivedStatus]);

  // ---- Assignment (brief) editing -----------------------------------------
  const [assignmentDraft, setAssignmentDraft] = useState<DraftAssignmentFormValue>(() =>
    toFormValue(draft, detail.brief)
  );
  const [assignmentErrors, setAssignmentErrors] = useState<Record<string, string>>({});
  const assignmentFormRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setAssignmentDraft(toFormValue(draft, detail.brief));
    setAssignmentErrors({});
    // Resync the local edit buffer only when the server's brief/tier actually
    // change underneath us (e.g. after `updateBriefMutation` succeeds).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail.brief, draft.tier]);

  const updateBriefMutation = useMutation({
    mutationFn: (payload: DraftUpdateRequest) => updateDraft(draftId, payload),
    onSuccess: () => {
      toast.success("Project updated.");
      queryClient.invalidateQueries({ queryKey: draftRoomKeys.detail(draftId) });
    },
    onError: (err) => {
      const info = parseDraftRoomError(err);
      if (info.status === 409) {
        toast.error("This project changed elsewhere. Reload the page and try again.");
        return;
      }
      toast.error(info.detail);
    },
  });

  function handleSaveAssignment() {
    const errors = validateDraftAssignmentForm(assignmentDraft, "edit");
    setAssignmentErrors(errors);
    if (Object.keys(errors).length > 0) {
      focusFirstInvalidDraftAssignmentField(assignmentFormRef.current, errors);
      return;
    }
    updateBriefMutation.mutate({
      lock_version: draft.lock_version,
      brief: assignmentDraft.brief,
      tier: assignmentDraft.tier,
    });
  }

  // ---- Compile --------------------------------------------------------------
  const readyInputCount = detail.inputs.filter((input) => input.parse_status === "ready").length;

  function compileBlockedReason(): string | null {
    if (!capabilities) return "Loading Draft Room capabilities…";
    if (capabilities.enabled === false || capabilities.compile_available === false) {
      return DRAFT_ROOM_DISABLED_MESSAGE;
    }
    if (archived) return "This project is archived. Restore it before compiling.";
    if (vaultRevoked) return READY_BLOCKER_LABELS.vault_access_revoked;
    if (vaultAccess !== "write") {
      return "You have read-only access to this project's vault. Compiling requires vault write permission.";
    }
    if (hasActiveJob) return "A newsroom run is already active for this project.";
    if (readyInputCount === 0) return "Add at least one parsed, ready source file before compiling.";
    const briefErrors = validateDraftAssignmentForm(toFormValue(draft, detail.brief), "edit");
    const firstInvalidKey = DRAFT_ASSIGNMENT_FIELD_ORDER.find((key) => key in briefErrors);
    if (firstInvalidKey) return briefErrors[firstInvalidKey];
    return null;
  }
  const blockedReason = compileBlockedReason();

  const [compileDialog, setCompileDialog] = useState<{
    startStage?: DraftCompileStartStage;
    idempotencyKey: string;
  } | null>(null);
  const compileHeadingRef = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    if (!compileDialog) return;
    const frame = requestAnimationFrame(() => compileHeadingRef.current?.focus());
    return () => cancelAnimationFrame(frame);
  }, [compileDialog]);

  function openCompileDialog(startStage?: DraftCompileStartStage) {
    setCompileDialog({ startStage, idempotencyKey: generateIdempotencyKey() });
  }

  useImperativeHandle(ref, () => ({ requestCompile: openCompileDialog }), []);

  const compileMutation = useMutation({
    mutationFn: (vars: { request: CompileRequest; idempotencyKey: string }) =>
      compileDraft(draftId, vars.request, vars.idempotencyKey),
    onSuccess: () => {
      toast.success("Compile started.");
      setCompileDialog(null);
      queryClient.invalidateQueries({ queryKey: draftRoomKeys.detail(draftId) });
      queryClient.invalidateQueries({ queryKey: draftRoomKeys.jobs(draftId) });
    },
    onError: (err) => toast.error(parseDraftRoomError(err).detail),
  });

  function submitCompile() {
    if (!compileDialog) return;
    const request: CompileRequest = {
      base_revision_id: currentRevisionId,
      lock_version: draft.lock_version,
      start_stage: compileDialog.startStage,
    };
    compileMutation.mutate({ request, idempotencyKey: compileDialog.idempotencyKey });
  }

  const stageOrder = capabilities?.compile_stage_order ?? [];
  function estimateStageCount(startStage?: DraftCompileStartStage): number {
    if (stageOrder.length === 0) return 0;
    if (!startStage) return stageOrder.length;
    const index = stageOrder.indexOf(startStage);
    return index === -1 ? stageOrder.length : stageOrder.length - index;
  }
  const maxModelCalls =
    typeof capabilities?.limits?.max_model_calls === "number" ? capabilities.limits.max_model_calls : null;

  // ---- Cancel ---------------------------------------------------------------
  const [cancelConfirmOpen, setCancelConfirmOpen] = useState(false);
  const cancelHeadingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (!cancelConfirmOpen) return;
    const frame = requestAnimationFrame(() => cancelHeadingRef.current?.focus());
    return () => cancelAnimationFrame(frame);
  }, [cancelConfirmOpen]);
  const cancelMutation = useMutation({
    mutationFn: () => cancelDraftJob(draftId, (activeJob as DraftJob).id),
    onSuccess: () => {
      toast.success("Cancellation requested.");
      setCancelConfirmOpen(false);
      queryClient.invalidateQueries({ queryKey: draftRoomKeys.detail(draftId) });
      queryClient.invalidateQueries({ queryKey: draftRoomKeys.jobs(draftId) });
    },
    onError: (err) => toast.error(parseDraftRoomError(err).detail),
  });

  // ---- Retry ------------------------------------------------------------
  // Never offers "assemble" or "intake": `capabilities.compile_start_stages`
  // (falling back to the client mirror `DRAFT_COMPILE_START_STAGES`) already
  // excludes both, so simply rendering its contents is enough to satisfy that
  // constraint without a manual filter.
  const [retryStage, setRetryStage] = useState<DraftCompileStartStage | "">("");
  const retryOptions = capabilities?.compile_start_stages ?? [];
  const canRetry = latestCompileJob != null && latestCompileJob.status === "failed" && canCompileOrPromote;

  const retryMutation = useMutation({
    mutationFn: (startStage: DraftCompileStartStage) =>
      retryDraftJob(draftId, (latestCompileJob as DraftJob).id, { start_stage: startStage }),
    onSuccess: (job, startStage) => {
      toast.success("Retry started.");
      if (job.start_stage && job.start_stage !== startStage) {
        const normalizedLabel = STAGE_LABELS[job.start_stage] ?? job.start_stage;
        toast.info(
          `The server started from an earlier stage (${normalizedLabel}) because a dependency needed to be recomputed.`
        );
      }
      setRetryStage("");
      queryClient.invalidateQueries({ queryKey: draftRoomKeys.detail(draftId) });
      queryClient.invalidateQueries({ queryKey: draftRoomKeys.jobs(draftId) });
    },
    onError: (err) => toast.error(parseDraftRoomError(err).detail),
  });

  // ---- Save revision -------------------------------------------------------
  const [saveConfirmOpen, setSaveConfirmOpen] = useState(false);
  const [saveConflict, setSaveConflict] = useState(false);
  const saveHeadingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (!saveConfirmOpen) return;
    const frame = requestAnimationFrame(() => saveHeadingRef.current?.focus());
    return () => cancelAnimationFrame(frame);
  }, [saveConfirmOpen]);
  const saveRevisionMutation = useMutation({
    mutationFn: () =>
      createDraftRevision(draftId, {
        base_revision_id: currentRevisionId,
        lock_version: draft.lock_version,
        content_md: editorValue,
      }),
    onSuccess: (revisionDetail) => {
      toast.success("New revision saved.");
      setSaveConfirmOpen(false);
      setSaveConflict(false);
      clearDraftTextStore(draftId);
      queryClient.invalidateQueries({ queryKey: draftRoomKeys.detail(draftId) });
      queryClient.invalidateQueries({ queryKey: draftRoomKeys.revisions(draftId) });
      queryClient.invalidateQueries({ queryKey: draftRoomKeys.revision(draftId, revisionDetail.summary.id) });
    },
    onError: (err) => {
      const info = parseDraftRoomError(err);
      if (info.status === 409) {
        setSaveConflict(true);
        return;
      }
      toast.error(info.detail);
    },
  });

  function handleReloadAfterConflict() {
    setSaveConflict(false);
    setSaveConfirmOpen(false);
    queryClient.invalidateQueries({ queryKey: draftRoomKeys.detail(draftId) });
    queryClient.invalidateQueries({ queryKey: draftRoomKeys.revisions(draftId) });
  }

  function handleCompareAfterConflict() {
    setCompareRevisions(currentRevisionId, null);
    setEditorTab("compare");
    setSaveConflict(false);
    setSaveConfirmOpen(false);
    queryClient.invalidateQueries({ queryKey: draftRoomKeys.detail(draftId) });
    queryClient.invalidateQueries({ queryKey: draftRoomKeys.revisions(draftId) });
  }

  // ---- Ready / Export / Promote ---------------------------------------------
  const [readyDialogOpen, setReadyDialogOpen] = useState(false);
  const [exportDialogOpen, setExportDialogOpen] = useState(false);
  const [promoteDialogOpen, setPromoteDialogOpen] = useState(false);

  // The server can disable promotion independently of vault-write access
  // (`capabilities.promote_available`); fail closed while capabilities are
  // still loading, matching `compileBlockedReason`'s convention.
  const promoteDisabled = !capabilities || capabilities.promote_available === false;
  const promoteUnavailableReason =
    capabilities && capabilities.promote_available === false
      ? "Promotion is currently unavailable."
      : null;

  const currentRevisionSummary = detail.current_revision_summary;
  const factStatusCurrent = currentRevisionSummary != null && FACT_CURRENT.has(currentRevisionSummary.fact_status);
  const isReadyRevision = draft.status === "ready" && currentRevisionSummary != null;

  const eligibility: ReadyEligibility = useMemo(() => {
    const blockers = new Set<string>();
    if (hasActiveJob) blockers.add("active_job");
    // A partial retrieval means the fact-check the server would need to point
    // to either never completed or covers stale evidence — treat it as the
    // same client-side blocker as "no current fact-check" rather than
    // inventing a code the server (and DraftReadyDialog's checklist) doesn't
    // recognise.
    if (!factStatusCurrent || retrievalPartial) blockers.add("fact_not_current");
    const blockingClaimCount = BLOCKING_CLAIM_STATUSES.reduce(
      (sum, status) => sum + (detail.claim_counts_by_status[status] ?? 0),
      0
    );
    if (blockingClaimCount > 0) blockers.add("unresolved_claim_blocker");
    if ((detail.finding_counts_by_severity.blocker ?? 0) > 0) blockers.add("unresolved_blocker");
    if (evidenceInvalidated) blockers.add("evidence_changed");
    if (vaultRevoked) blockers.add("vault_access_revoked");
    const blockersArray = Array.from(blockers);
    return { ok: blockersArray.length === 0, blockers: blockersArray };
  }, [
    hasActiveJob,
    factStatusCurrent,
    retrievalPartial,
    detail.claim_counts_by_status,
    detail.finding_counts_by_severity,
    evidenceInvalidated,
    vaultRevoked,
  ]);

  // ---- Delete / Restore ---------------------------------------------------
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  const deleteHeadingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (!deleteConfirmOpen) return;
    const frame = requestAnimationFrame(() => deleteHeadingRef.current?.focus());
    return () => cancelAnimationFrame(frame);
  }, [deleteConfirmOpen]);
  const deleteMutation = useMutation({
    mutationFn: () => deleteDraft(draftId),
    onSuccess: () => {
      toast.success("Project deleted.");
      queryClient.invalidateQueries({ queryKey: draftRoomKeys.lists() });
      navigate("/draft-room");
    },
    onError: (err) => toast.error(parseDraftRoomError(err).detail),
  });
  const restoreMutation = useMutation({
    mutationFn: () => restoreDraft(draftId, draft.lock_version),
    onSuccess: () => {
      toast.success("Project restored.");
      queryClient.invalidateQueries({ queryKey: draftRoomKeys.detail(draftId) });
    },
    onError: (err) => toast.error(parseDraftRoomError(err).detail),
  });

  const [archiveConfirmOpen, setArchiveConfirmOpen] = useState(false);
  const archiveHeadingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (!archiveConfirmOpen) return;
    const frame = requestAnimationFrame(() => archiveHeadingRef.current?.focus());
    return () => cancelAnimationFrame(frame);
  }, [archiveConfirmOpen]);
  const archiveBlockedReason = hasActiveJob
    ? "Archiving is unavailable while a newsroom run is active."
    : null;
  const archiveMutation = useMutation({
    mutationFn: () => archiveDraft(draftId, draft.lock_version),
    onSuccess: () => {
      toast.success("Project archived.");
      setArchiveConfirmOpen(false);
      queryClient.invalidateQueries({ queryKey: draftRoomKeys.detail(draftId) });
      queryClient.invalidateQueries({ queryKey: draftRoomKeys.lists() });
    },
    onError: (err) => toast.error(parseDraftRoomError(err).detail),
  });

  // ---------------------------------------------------------------------------

  const assignmentPanel = (
    <div className="space-y-4">
      <div ref={assignmentFormRef}>
        <DraftAssignmentForm
          value={assignmentDraft}
          onChange={setAssignmentDraft}
          errors={assignmentErrors}
          variant="edit"
          disabled={!canManageContent || updateBriefMutation.isPending}
          idPrefix={`draft-${draftId}-assignment`}
          capabilities={capabilities}
          inputs={detail.inputs}
        />
      </div>
      {detail.inputs.length === 0 && (
        <Alert variant="warning">
          <AlertTitle>No source files yet</AlertTitle>
          <AlertDescription className="flex flex-wrap items-center justify-between gap-2">
            <span>Add at least one source file before compiling.</span>
            <Button type="button" size="sm" variant="outline" onClick={() => setWorkspaceTab("sources")}>
              Go to Sources
            </Button>
          </AlertDescription>
        </Alert>
      )}
      {canManageContent && (
        <Button type="button" onClick={handleSaveAssignment} disabled={updateBriefMutation.isPending}>
          {updateBriefMutation.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />}
          Save brief
        </Button>
      )}
    </div>
  );

  const sourcesPanel = (
    <div className="space-y-4">
      <DraftSourceUpload
        draftId={draftId}
        disabled={!canManageContent || hasActiveJob}
        disabledReason={
          !canManageContent
            ? archived
              ? "This project is archived and read-only."
              : READY_BLOCKER_LABELS.vault_access_revoked
            : hasActiveJob
              ? "Editing is unavailable while a newsroom run is active."
              : undefined
        }
        maxInputs={typeof capabilities?.limits?.max_inputs === "number" ? capabilities.limits.max_inputs : 50}
        currentInputCount={detail.inputs.length}
      />
      <DraftSourceList
        draftId={draftId}
        inputs={detail.inputs}
        locked={hasActiveJob}
        lockedReason={activeJob ? STAGE_LABELS[activeJob.active_stage ?? ""] : undefined}
        canEdit={canManageContent}
      />
    </div>
  );

  function renderWorkspaceTabContent(tab: WorkspaceTab) {
    if (tab === "assignment") return assignmentPanel;
    if (tab === "sources") return sourcesPanel;
    if (STAGE_VIEW_TABS.has(tab)) {
      return <DraftStageArtifact stage={findLatestStage(stages, tab as DraftStageName)} />;
    }
    // tab === "draft": the manuscript editor / preview / compare area.
    return (
      <div className="space-y-4">
        <Tabs value={editorTab} onValueChange={(value) => setEditorTab(value as EditorTab)}>
          <TabsList>
            <TabsTrigger value="editor">Editor</TabsTrigger>
            <TabsTrigger value="preview">Preview</TabsTrigger>
            <TabsTrigger value="compare">Compare</TabsTrigger>
          </TabsList>
          <TabsContent value="editor">
            <DraftEditor
              draftId={draftId}
              revision={currentRevisionDetailQuery.data ?? null}
              value={editorValue}
              onChange={(next) => setDraftTextStore(draftId, next)}
              disabled={!canManageContent}
              disabledReason={
                archived
                  ? "This project is archived and read-only."
                  : vaultRevoked
                    ? READY_BLOCKER_LABELS.vault_access_revoked
                    : undefined
              }
            />
            {canManageContent && (
              <div className="mt-3 space-y-2">
                {isDirty && <p className="text-xs text-muted-foreground">{SAVE_REVISION_CONSEQUENCE}</p>}
                <Button type="button" onClick={() => setSaveConfirmOpen(true)} disabled={!isDirty}>
                  {SAVE_REVISION_CTA}
                </Button>
              </div>
            )}
          </TabsContent>
          <TabsContent value="preview">
            <DraftPreview content={editorValue} />
          </TabsContent>
          <TabsContent value="compare">
            <DraftRevisionDiff
              revisions={revisions}
              fromRevisionId={compareFromRevisionId}
              toRevisionId={compareToRevisionId}
              onSelectFrom={(id) => setCompareRevisions(id, compareToRevisionId)}
              onSelectTo={(id) => setCompareRevisions(compareFromRevisionId, id)}
              fromContent={compareFromQuery.data?.content_md ?? null}
              toContent={compareToQuery.data?.content_md ?? null}
              loading={compareFromQuery.isFetching || compareToQuery.isFetching}
            />
          </TabsContent>
        </Tabs>
      </div>
    );
  }

  const readyApprovalMessage = readyApprovalText(draft);

  const runControls = (
    <div className="flex flex-col gap-2 rounded-md border border-border p-3">
      {readyApprovalMessage && currentRevisionSummary && (
        <p className="text-sm text-success" data-testid="ready-approval">
          {readyApprovalMessage} &middot; Revision {currentRevisionSummary.revision_no} &middot;{" "}
          {currentRevisionSummary.content_sha256.slice(0, 12)}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Button
          type="button"
          onClick={() => openCompileDialog(undefined)}
          disabled={blockedReason != null}
        >
          {compileCtaLabel(draft.mode)}
        </Button>
        {hasActiveJob && (
          <Button type="button" variant="outline" onClick={() => setCancelConfirmOpen(true)}>
            Cancel run
          </Button>
        )}
        {archived && vaultAccess === "write" && (
          <Button type="button" variant="outline" onClick={() => restoreMutation.mutate()} disabled={restoreMutation.isPending}>
            {restoreMutation.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />}
            Restore project
          </Button>
        )}
        {!archived && (
          <Button
            type="button"
            variant="outline"
            onClick={() => setArchiveConfirmOpen(true)}
            disabled={archiveBlockedReason != null}
          >
            Archive project
          </Button>
        )}
        {currentRevisionSummary && canCompileOrPromote && (
          <Button type="button" variant="outline" onClick={() => setReadyDialogOpen(true)}>
            {MARK_READY_CTA}
          </Button>
        )}
        {currentRevisionSummary && !vaultRevoked && (
          <Button type="button" variant="outline" onClick={() => setExportDialogOpen(true)}>
            {EXPORT_CTA}
          </Button>
        )}
        {canCompileOrPromote && (
          <Button
            type="button"
            variant="outline"
            onClick={() => setPromoteDialogOpen(true)}
            disabled={promoteDisabled}
          >
            {PROMOTE_TO_VAULT_CTA}
          </Button>
        )}
        <Button type="button" variant="destructive" onClick={() => setDeleteConfirmOpen(true)}>
          Delete project
        </Button>
      </div>
      {blockedReason != null && <p className="text-sm text-muted-foreground">{blockedReason}</p>}
      {archiveBlockedReason != null && (
        <p className="text-sm text-muted-foreground">{archiveBlockedReason}</p>
      )}
      {canCompileOrPromote && promoteUnavailableReason != null && (
        <p className="text-sm text-muted-foreground">{promoteUnavailableReason}</p>
      )}

      {canRetry && (
        <div className="flex flex-wrap items-center gap-2 border-t border-border pt-2">
          <Label htmlFor={`draft-${draftId}-retry-stage`} className="mb-0">
            Retry from
          </Label>
          <Select value={retryStage} onValueChange={(value) => setRetryStage(value as DraftCompileStartStage)}>
            <SelectTrigger id={`draft-${draftId}-retry-stage`} className="w-48">
              <SelectValue placeholder="Choose a stage" />
            </SelectTrigger>
            <SelectContent>
              {retryOptions.map((stage) => (
                <SelectItem key={stage} value={stage}>
                  {STAGE_LABELS[stage] ?? stage}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button
            type="button"
            size="sm"
            onClick={() => retryStage && retryMutation.mutate(retryStage)}
            disabled={!retryStage || retryMutation.isPending}
          >
            {retryMutation.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />}
            Retry
          </Button>
        </div>
      )}
    </div>
  );

  return (
    <div className="min-w-0 space-y-4">
      <DraftStageRail
        stageOrder={stageOrder}
        stages={stages}
        activeStage={relevantJobActiveStage}
        selectedStage={selectedStage}
        onSelectStage={handleSelectStage}
        jobStatus={relevantJobStatus}
        blockerCountsByStage={blockerCountsByStage}
        warningCountsByStage={warningCountsByStage}
      />

      {runControls}

      <div className="grid gap-4 lg:grid-cols-[1fr_360px]">
        <div className="min-w-0 space-y-4">
          {/* A single tab bar at every breakpoint — duplicating it per breakpoint
              (one hidden via CSS below `lg`, one above) would leave two live
              tablists in the DOM at once, which is both wasted markup and a
              trap for assistive tech that doesn't honour the same media query
              context. The bar scrolls horizontally instead of wrapping to a
              second row, so it never causes page-level horizontal overflow. */}
          <nav aria-label="Draft sections">
            <Tabs value={workspaceTab} onValueChange={(value) => setWorkspaceTab(value as WorkspaceTab)}>
              <TabsList className="h-auto w-full flex-nowrap justify-start overflow-x-auto">
                {WORKSPACE_TAB_ITEMS.map((item) => (
                  <TabsTrigger key={item.value} value={item.value} className="shrink-0">
                    {item.label}
                    {item.value === "sources" && detail.inputs.length > 0 ? ` (${detail.inputs.length})` : ""}
                  </TabsTrigger>
                ))}
              </TabsList>
              {WORKSPACE_TAB_ITEMS.map((item) => (
                <TabsContent key={item.value} value={item.value}>
                  {renderWorkspaceTabContent(item.value)}
                </TabsContent>
              ))}
            </Tabs>
          </nav>

          {/* Mobile full-screen quick actions for Sources and Evidence, so
              small screens don't have to scroll the tab bar and the inspector
              to reach either. */}
          <div className="flex gap-2 sm:hidden">
            <Button type="button" variant="outline" onClick={() => setSourcesSheetOpen(true)}>
              Add sources
            </Button>
            <Button
              type="button"
              variant="outline"
              onClick={() => {
                setInspectorTab("evidence");
                setEvidenceSheetOpen(true);
              }}
            >
              View evidence
            </Button>
          </div>
          <Sheet open={sourcesSheetOpen} onOpenChange={setSourcesSheetOpen}>
            <SheetContent side="bottom" className="h-full w-full overflow-y-auto sm:max-w-full">
              <SheetHeader>
                <SheetTitle>Sources</SheetTitle>
              </SheetHeader>
              <div className="mt-4">{sourcesPanel}</div>
            </SheetContent>
          </Sheet>
          <Sheet open={evidenceSheetOpen} onOpenChange={setEvidenceSheetOpen}>
            <SheetContent side="bottom" className="h-full w-full overflow-y-auto sm:max-w-full">
              <SheetHeader>
                <SheetTitle>Evidence</SheetTitle>
              </SheetHeader>
              <div className="mt-4">
                <DraftEvidencePanel draftId={draftId} jobId={inspectorJobId} />
              </div>
            </SheetContent>
          </Sheet>
        </div>

        <DraftInspector
          draftId={draftId}
          revisionId={currentRevisionId}
          jobId={inspectorJobId}
          stage={selectedStageEntry}
          lockVersion={draft.lock_version}
          canDispose={canManageContent && !hasActiveJob}
          tier={draft.tier}
        />
      </div>

      {/* ---- Compile confirmation ---- */}
      <Dialog open={compileDialog != null} onOpenChange={(open) => !open && setCompileDialog(null)}>
        <DialogContent aria-describedby="draft-compile-description">
          <DialogHeader>
            <DialogTitle ref={compileHeadingRef} tabIndex={-1}>
              {compileCtaLabel(draft.mode)}
            </DialogTitle>
            <DialogDescription id="draft-compile-description">{PROVIDER_DISCLOSURE}</DialogDescription>
          </DialogHeader>
          <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
            <dt className="text-muted-foreground">Mode</dt>
            <dd>{MODE_LABELS[draft.mode] ?? draft.mode}</dd>
            <dt className="text-muted-foreground">Tier</dt>
            <dd>{TIER_LABELS[draft.tier] ?? draft.tier}</dd>
            <dt className="text-muted-foreground">Sources</dt>
            <dd>
              {detail.inputs.length} ({summarizeRoles(detail.inputs)})
            </dd>
            <dt className="text-muted-foreground">Estimated stages</dt>
            <dd>{estimateStageCount(compileDialog?.startStage)}</dd>
            <dt className="text-muted-foreground">Model-call limit</dt>
            <dd>{maxModelCalls ?? "Not reported by the server"}</dd>
          </dl>
          <p className="text-sm text-muted-foreground">
            This result requires human review before it can be marked Ready.
          </p>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setCompileDialog(null)}>
              Cancel
            </Button>
            <Button type="button" onClick={submitCompile} disabled={compileMutation.isPending}>
              {compileMutation.isPending && (
                <Loader2 className="mr-2 h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
              )}
              {compileCtaLabel(draft.mode)}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ---- Cancel confirmation ---- */}
      <Dialog open={cancelConfirmOpen} onOpenChange={setCancelConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle ref={cancelHeadingRef} tabIndex={-1}>
              Cancel this run?
            </DialogTitle>
            <DialogDescription>{CANCEL_CONSEQUENCE}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => setCancelConfirmOpen(false)}>
              Keep running
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={() => cancelMutation.mutate()}
              disabled={cancelMutation.isPending}
            >
              {cancelMutation.isPending && (
                <Loader2 className="mr-2 h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
              )}
              Cancel run
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ---- Save revision confirmation / conflict ---- */}
      <Dialog
        open={saveConfirmOpen}
        onOpenChange={(open) => {
          setSaveConfirmOpen(open);
          if (!open) setSaveConflict(false);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle ref={saveHeadingRef} tabIndex={-1}>
              Save a new revision?
            </DialogTitle>
            <DialogDescription>{SAVE_REVISION_CONSEQUENCE}</DialogDescription>
          </DialogHeader>
          {saveConflict ? (
            <Alert variant="destructive">
              <AlertTitle>Conflict</AlertTitle>
              <AlertDescription className="space-y-2">
                <p>{READY_BLOCKER_LABELS.conflict}</p>
                <div className="flex gap-2">
                  <Button type="button" size="sm" variant="outline" onClick={handleReloadAfterConflict}>
                    <RotateCcw className="mr-1 h-3.5 w-3.5" aria-hidden="true" />
                    Reload
                  </Button>
                  <Button type="button" size="sm" variant="outline" onClick={handleCompareAfterConflict}>
                    Compare
                  </Button>
                </div>
              </AlertDescription>
            </Alert>
          ) : (
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setSaveConfirmOpen(false)}>
                Cancel
              </Button>
              <Button
                type="button"
                onClick={() => saveRevisionMutation.mutate()}
                disabled={saveRevisionMutation.isPending}
              >
                {saveRevisionMutation.isPending && (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
                )}
                {SAVE_REVISION_CTA}
              </Button>
            </DialogFooter>
          )}
        </DialogContent>
      </Dialog>

      {/* ---- Delete confirmation ---- */}
      <Dialog open={deleteConfirmOpen} onOpenChange={setDeleteConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle ref={deleteHeadingRef} tabIndex={-1}>
              Delete this project?
            </DialogTitle>
            <DialogDescription>
              This permanently deletes the project, its sources, and its revisions. This cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => setDeleteConfirmOpen(false)}>
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={() => deleteMutation.mutate()}
              disabled={deleteMutation.isPending}
            >
              {deleteMutation.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />}
              Delete project
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ---- Archive confirmation ---- */}
      <Dialog open={archiveConfirmOpen} onOpenChange={setArchiveConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle ref={archiveHeadingRef} tabIndex={-1}>
              Archive this project?
            </DialogTitle>
            <DialogDescription>
              Archiving makes this project read-only until it is restored. Its sources and revisions
              are kept.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => setArchiveConfirmOpen(false)}>
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={() => archiveMutation.mutate()}
              disabled={archiveMutation.isPending}
            >
              {archiveMutation.isPending && (
                <Loader2 className="mr-2 h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
              )}
              Archive project
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {currentRevisionSummary && (
        <DraftReadyDialog
          open={readyDialogOpen}
          onOpenChange={setReadyDialogOpen}
          draft={draft}
          revision={currentRevisionSummary}
          eligibility={eligibility}
          requiresSourceOnlyAck={sourceOnly}
          onReady={() => {
            queryClient.invalidateQueries({ queryKey: draftRoomKeys.detail(draftId) });
          }}
        />
      )}

      {currentRevisionSummary && (
        <DraftExportDialog
          open={exportDialogOpen}
          onOpenChange={setExportDialogOpen}
          draftId={draftId}
          revision={currentRevisionSummary}
          isReadyRevision={isReadyRevision}
        />
      )}

      <DraftPromoteDialog
        open={promoteDialogOpen}
        onOpenChange={setPromoteDialogOpen}
        draft={draft}
        inputs={detail.inputs}
        revisions={revisions}
        currentRevisionId={currentRevisionId}
        canWrite={canCompileOrPromote}
        onPromoted={(result: PromoteResponse) => {
          toast.success(`Promoted to the vault as ${result.filename}.`);
        }}
      />
    </div>
  );
});
