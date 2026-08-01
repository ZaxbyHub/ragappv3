import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { AlertTriangle, Info, Loader2, RotateCcw, XCircle } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Pagination } from "@/components/ui/pagination";
import { Separator } from "@/components/ui/separator";
import { Textarea } from "@/components/ui/textarea";
import {
  DRAFT_FINDING_SEVERITIES,
  DRAFT_FINDING_STATUSES,
  draftRoomKeys,
  listDraftFindings,
  parseDraftRoomError,
  setDraftFindingDisposition,
  type DraftFinding,
  type DraftFindingSeverity,
  type DraftFindingStatus,
  type DraftRevisionSummary,
  type DraftTier,
  type FindingDispositionRequest,
} from "@/lib/api/draftRoom";
import {
  ARCHIVED_READ_ONLY_WARNING,
  READY_BLOCKER_LABELS,
  STAGE_LABELS,
  TIER_DESCRIPTIONS,
  VAULT_ACCESS_REVOKED_WARNING,
} from "@/components/draft-room/labels";
import { useDraftRoomUiStore } from "@/stores/useDraftRoomUiStore";
import { cn } from "@/lib/utils";

export interface DraftFindingsPanelProps {
  draftId: number;
  revisionId: number | null;
  lockVersion: number;
  baseRevisionId: number | null;
  /** False when archived, vault revoked, or a job is active. */
  canDispose: boolean;
  /** Drives the tier-specific consequence shown before confirming a waiver. */
  tier: DraftTier;
  onRevisionCreated?(revision: DraftRevisionSummary): void;
}

const DEFAULT_PER_PAGE = 20;

const SEVERITY_META: Record<
  DraftFindingSeverity,
  { label: string; className: string; Icon: typeof Info }
> = {
  info: { label: "Info", className: "border-border bg-muted text-muted-foreground", Icon: Info },
  warning: {
    label: "Warning",
    className: "border-warning/50 bg-warning/10 text-warning",
    Icon: AlertTriangle,
  },
  blocker: {
    label: "Blocker",
    className: "border-destructive/50 bg-destructive/10 text-destructive",
    Icon: XCircle,
  },
};

const STATUS_LABEL: Record<DraftFindingStatus, string> = {
  open: "Open",
  applied: "Applied",
  dismissed: "Dismissed",
  waived: "Waived",
  resolved_by_revision: "Resolved by revision",
};

/**
 * Tier-independent part of the waiver consequence (SPEC 12.5 rule 6 —
 * waivers become invalid if the flagged text or rule version changes).
 * Rendered alongside `TIER_DESCRIPTIONS[tier]`, which states what a
 * single-source high-stakes claim actually costs at this project's tier.
 */
const WAIVE_CONSEQUENCE =
  `Waiving records that a human accepted this finding without changing the text. ` +
  READY_BLOCKER_LABELS.stale_waiver;

function dispositionSuccessMessage(action: FindingDispositionRequest["action"]): string {
  if (action === "apply") return "Finding applied — a new revision was created.";
  if (action === "dismiss") return "Finding dismissed.";
  return "Finding waived.";
}

interface FilterGroupProps<T extends string> {
  label: string;
  options: ReadonlyArray<{ value: T | null; label: string }>;
  value: T | null;
  onChange(value: T | null): void;
}

function FilterGroup<T extends string>({ label, options, value, onChange }: FilterGroupProps<T>) {
  return (
    <div>
      <span className="mb-1 block text-xs font-medium text-muted-foreground">{label}</span>
      <div role="group" aria-label={label} className="flex flex-wrap gap-1.5">
        {options.map((option) => {
          const isActive = option.value === value;
          return (
            <button
              key={option.label}
              type="button"
              aria-pressed={isActive}
              onClick={() => onChange(option.value)}
              className={cn(
                "rounded-full border px-2.5 py-1 text-xs font-medium transition-colors",
                isActive
                  ? "border-primary bg-primary text-primary-foreground"
                  : "border-border bg-muted text-muted-foreground hover:bg-accent"
              )}
            >
              {option.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

interface FindingRowProps {
  finding: DraftFinding;
  tier: DraftTier;
  canDispose: boolean;
  conflict: boolean;
  isMutating: boolean;
  waiveOpen: boolean;
  waiveReason: string;
  onWaiveReasonChange(value: string): void;
  onStartWaive(): void;
  onCancelWaive(): void;
  onApply(): void;
  onDismiss(): void;
  onConfirmWaive(): void;
}

function FindingRow({
  finding,
  tier,
  canDispose,
  conflict,
  isMutating,
  waiveOpen,
  waiveReason,
  onWaiveReasonChange,
  onStartWaive,
  onCancelWaive,
  onApply,
  onDismiss,
  onConfirmWaive,
}: FindingRowProps) {
  const severityMeta = SEVERITY_META[finding.severity];
  const SeverityIcon = severityMeta.Icon;
  const actionsDisabled = !canDispose || conflict || isMutating;
  const reasonId = `waive-reason-${finding.id}`;
  const reasonHintId = `waive-reason-hint-${finding.id}`;

  return (
    <li className="rounded-sm border border-border p-4">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="outline" className={severityMeta.className}>
          <SeverityIcon className="mr-1 h-3 w-3" aria-hidden="true" />
          {severityMeta.label}
        </Badge>
        <Badge variant="secondary">{STATUS_LABEL[finding.status]}</Badge>
        <span className="text-xs text-muted-foreground">
          {(STAGE_LABELS[finding.stage] ?? finding.stage) +
            " · " +
            finding.category +
            " · " +
            finding.rule_id +
            " v" +
            finding.rule_version}
        </span>
      </div>

      <p className="mt-2 text-sm text-foreground">{finding.message}</p>

      {(finding.original_text != null || finding.suggestion != null) && (
        <div className="mt-2 space-y-1 overflow-x-auto rounded-sm bg-muted/50 p-2 text-xs">
          {finding.original_text != null && (
            <p>
              <span className="font-medium text-muted-foreground">Current text: </span>
              <span className="font-mono">{finding.original_text}</span>
            </p>
          )}
          {finding.suggestion != null && (
            <p>
              <span className="font-medium text-muted-foreground">Suggested text: </span>
              <span className="font-mono">{finding.suggestion}</span>
            </p>
          )}
        </div>
      )}

      {finding.resolution_note != null && (
        <p className="mt-2 text-xs text-muted-foreground">Note: {finding.resolution_note}</p>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-2">
        {finding.can_apply && (
          <Button
            type="button"
            size="sm"
            variant="default"
            disabled={actionsDisabled}
            onClick={onApply}
          >
            Apply
          </Button>
        )}
        {finding.can_dismiss && (
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={actionsDisabled}
            onClick={onDismiss}
          >
            Dismiss
          </Button>
        )}
        {finding.can_waive && !waiveOpen && (
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={actionsDisabled}
            onClick={onStartWaive}
          >
            Waive
          </Button>
        )}
      </div>

      {finding.can_waive && waiveOpen && (
        <div className="mt-3 space-y-2 rounded-sm border border-border bg-muted/30 p-3">
          <Label htmlFor={reasonId}>Reason for waiver</Label>
          <Textarea
            id={reasonId}
            value={waiveReason}
            onChange={(event) => onWaiveReasonChange(event.target.value)}
            aria-describedby={reasonHintId}
            aria-invalid={waiveReason.trim().length === 0}
            required
            rows={2}
          />
          <p id={reasonHintId} className="text-xs text-muted-foreground">
            {WAIVE_CONSEQUENCE} {TIER_DESCRIPTIONS[tier]}
          </p>
          <div className="flex gap-2">
            <Button
              type="button"
              size="sm"
              variant="default"
              disabled={actionsDisabled || waiveReason.trim().length === 0}
              onClick={onConfirmWaive}
            >
              {isMutating ? <Loader2 className="mr-1 h-3 w-3 animate-spin motion-reduce:animate-none" aria-hidden="true" /> : null}
              Confirm waiver
            </Button>
            <Button type="button" size="sm" variant="ghost" onClick={onCancelWaive}>
              Cancel
            </Button>
          </div>
        </div>
      )}
    </li>
  );
}

export function DraftFindingsPanel({
  draftId,
  revisionId,
  lockVersion,
  baseRevisionId,
  canDispose,
  tier,
  onRevisionCreated,
}: DraftFindingsPanelProps) {
  const queryClient = useQueryClient();
  const severityFilter = useDraftRoomUiStore((s) => s.findingSeverityFilter) as DraftFindingSeverity | null;
  const statusFilter = useDraftRoomUiStore((s) => s.findingStatusFilter) as DraftFindingStatus | null;
  const setSeverityFilter = useDraftRoomUiStore((s) => s.setFindingSeverityFilter);
  const setStatusFilter = useDraftRoomUiStore((s) => s.setFindingStatusFilter);

  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(DEFAULT_PER_PAGE);
  const [conflict, setConflict] = useState(false);
  const [waivingFindingId, setWaivingFindingId] = useState<number | null>(null);
  const [waiveReason, setWaiveReason] = useState("");

  useEffect(() => {
    setPage(1);
  }, [severityFilter, statusFilter, revisionId]);

  const findingsParams = {
    status: statusFilter ?? undefined,
    severity: severityFilter ?? undefined,
    revision_id: revisionId ?? undefined,
    page,
    per_page: perPage,
  };

  const findingsQuery = useQuery({
    queryKey: draftRoomKeys.findings(draftId, findingsParams),
    queryFn: () => listDraftFindings(draftId, findingsParams),
  });

  const dispositionMutation = useMutation({
    mutationFn: ({
      finding,
      action,
      note,
    }: {
      finding: DraftFinding;
      action: FindingDispositionRequest["action"];
      note?: string;
    }) =>
      setDraftFindingDisposition(draftId, finding.id, {
        action,
        base_revision_id: baseRevisionId,
        lock_version: lockVersion,
        ...(note ? { note } : {}),
      }),
    onSuccess: (response, variables) => {
      queryClient.invalidateQueries({ queryKey: draftRoomKeys.detail(draftId) });
      queryClient.invalidateQueries({ queryKey: draftRoomKeys.revisions(draftId) });
      queryClient.invalidateQueries({ queryKey: draftRoomKeys.findings(draftId) });
      queryClient.invalidateQueries({ queryKey: draftRoomKeys.claims(draftId) });
      setWaivingFindingId(null);
      setWaiveReason("");
      toast.success(dispositionSuccessMessage(variables.action));
      if (response.revision) onRevisionCreated?.(response.revision);
    },
    onError: (err) => {
      const info = parseDraftRoomError(err);
      if (info.status === 409) {
        setConflict(true);
        return;
      }
      toast.error(info.detail);
    },
  });

  const handleReload = () => {
    setConflict(false);
    queryClient.invalidateQueries({ queryKey: draftRoomKeys.detail(draftId) });
    queryClient.invalidateQueries({ queryKey: draftRoomKeys.findings(draftId) });
  };

  const items = findingsQuery.data?.items ?? [];
  const total = findingsQuery.data?.total ?? 0;

  return (
    <div className="space-y-4">
      {conflict && (
        <Alert variant="destructive">
          <AlertTitle>Conflict</AlertTitle>
          <AlertDescription className="flex items-center justify-between gap-3">
            <span>{READY_BLOCKER_LABELS.conflict}</span>
            <Button type="button" size="sm" variant="outline" onClick={handleReload}>
              <RotateCcw className="mr-1 h-3 w-3" aria-hidden="true" />
              Reload
            </Button>
          </AlertDescription>
        </Alert>
      )}

      {!canDispose && (
        <Alert variant="warning">
          <AlertTitle>Finding actions are unavailable</AlertTitle>
          <AlertDescription>
            <ul className="list-disc space-y-1 pl-4">
              <li>{ARCHIVED_READ_ONLY_WARNING}</li>
              <li>{VAULT_ACCESS_REVOKED_WARNING}</li>
              <li>{READY_BLOCKER_LABELS.active_job}</li>
            </ul>
          </AlertDescription>
        </Alert>
      )}

      <div className="flex flex-wrap gap-6">
        <FilterGroup<DraftFindingSeverity>
          label="Severity"
          value={severityFilter}
          onChange={setSeverityFilter}
          options={[
            { value: null, label: "All" },
            ...DRAFT_FINDING_SEVERITIES.map((severity) => ({
              value: severity,
              label: SEVERITY_META[severity].label,
            })),
          ]}
        />
        <FilterGroup<DraftFindingStatus>
          label="Status"
          value={statusFilter}
          onChange={setStatusFilter}
          options={[
            { value: null, label: "All" },
            ...DRAFT_FINDING_STATUSES.map((status) => ({
              value: status,
              label: STATUS_LABEL[status],
            })),
          ]}
        />
      </div>

      <Separator />

      {findingsQuery.isPending && <p className="text-sm text-muted-foreground">Loading findings…</p>}
      {findingsQuery.isError && (
        <Alert variant="destructive">
          <AlertTitle>Could not load findings</AlertTitle>
          <AlertDescription>{parseDraftRoomError(findingsQuery.error).detail}</AlertDescription>
        </Alert>
      )}
      {findingsQuery.isSuccess && items.length === 0 && (
        <p className="text-sm text-muted-foreground">No findings match the current filters.</p>
      )}

      {items.length > 0 && (
        <ul className="space-y-3">
          {items.map((finding) => (
            <FindingRow
              key={finding.id}
              finding={finding}
              tier={tier}
              canDispose={canDispose}
              conflict={conflict}
              isMutating={
                dispositionMutation.isPending &&
                dispositionMutation.variables?.finding.id === finding.id
              }
              waiveOpen={waivingFindingId === finding.id}
              waiveReason={waiveReason}
              onWaiveReasonChange={setWaiveReason}
              onStartWaive={() => {
                setWaivingFindingId(finding.id);
                setWaiveReason("");
              }}
              onCancelWaive={() => {
                setWaivingFindingId(null);
                setWaiveReason("");
              }}
              onApply={() => dispositionMutation.mutate({ finding, action: "apply" })}
              onDismiss={() => dispositionMutation.mutate({ finding, action: "dismiss" })}
              onConfirmWaive={() =>
                dispositionMutation.mutate({
                  finding,
                  action: "waive",
                  note: waiveReason.trim(),
                })
              }
            />
          ))}
        </ul>
      )}

      {total > 0 && (
        <Pagination
          page={page}
          limit={perPage}
          total={total}
          onPageChange={setPage}
          onLimitChange={setPerPage}
          isLoading={findingsQuery.isFetching}
          itemName="findings"
        />
      )}
    </div>
  );
}
