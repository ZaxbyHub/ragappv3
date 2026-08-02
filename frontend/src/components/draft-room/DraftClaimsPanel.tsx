import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, XCircle } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Pagination } from "@/components/ui/pagination";
import { Separator } from "@/components/ui/separator";
import {
  DRAFT_CLAIM_STATUSES,
  draftRoomKeys,
  listDraftClaims,
  listDraftEvidence,
  listDraftRevisions,
  parseDraftRoomError,
  type DraftClaim,
  type DraftClaimSource,
  type DraftClaimStatus,
  type DraftEvidence,
} from "@/lib/api/draftRoom";
import {
  CLAIM_STATUS_LABELS,
  INPUT_AUTHORITY_LABELS,
  SUPPORTED_BY_EVIDENCE,
  lexicalOverlapText,
} from "@/components/draft-room/labels";
import { useDraftRoomUiStore } from "@/stores/useDraftRoomUiStore";
import { cn } from "@/lib/utils";

export interface DraftClaimsPanelProps {
  draftId: number;
  revisionId: number | null;
}

const DEFAULT_PER_PAGE = 20;
// The documented API maximum per_page (§8.1 `limits.max_page_size`).
const EVIDENCE_LOOKUP_PAGE_SIZE = 100;
// Bound on how many evidence pages we'll scan per revision before giving up
// and showing an honest "could not resolve every source" note instead of
// silently mislabeling sources as "Unknown". 5 pages * 100 rows = 500 rows,
// comfortably above what a single compile job's Research stage produces in
// ordinary use.
const MAX_EVIDENCE_LOOKUP_PAGES = 5;
const REVISION_LOOKUP_PAGE_SIZE = 100;

const SOURCE_KIND_LABELS: Record<string, string> = {
  draft_input: "Project input",
  document: "Vault document",
  wiki: "Wiki",
  kms: "Knowledge base",
};

const SEVERITY_BADGE_CLASS: Record<string, string> = {
  warning: "border-warning/50 bg-warning/10 text-warning",
  blocker: "border-destructive/50 bg-destructive/10 text-destructive",
};

function isUnsupportedOrBlocker(claim: DraftClaim): boolean {
  return claim.status === "unsupported" || claim.severity === "blocker";
}

interface EvidenceLookupResult {
  items: DraftEvidence[];
  /** True if the page bound was hit before every evidence row was fetched. */
  truncated: boolean;
}

/**
 * Pages through a draft's evidence — scoped to `jobId` when known — until
 * every row is fetched (per the paginated envelope's `total`) or `maxPages`
 * is hit. Never silently stops early without reporting it: callers use
 * `truncated` to render an honest note instead of guessing "Unknown" for
 * every source past the bound.
 */
async function fetchEvidenceForLookup(
  draftId: number,
  jobId: number | undefined,
  maxPages: number
): Promise<EvidenceLookupResult> {
  const items: DraftEvidence[] = [];
  let page = 1;
  let total = Number.POSITIVE_INFINITY;
  while (page <= maxPages && items.length < total) {
    const response = await listDraftEvidence(draftId, {
      job_id: jobId,
      page,
      per_page: EVIDENCE_LOOKUP_PAGE_SIZE,
    });
    items.push(...response.items);
    total = response.total;
    if (response.items.length === 0) break;
    page += 1;
  }
  return { items, truncated: items.length < total };
}

function safeJsonStringify(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return "Unable to render the retrieval audit as JSON.";
  }
}

interface ClaimSourceRowProps {
  source: DraftClaimSource;
  evidenceById: Map<number, DraftEvidence>;
}

function ClaimSourceRow({ source, evidenceById }: ClaimSourceRowProps) {
  const evidence = evidenceById.get(source.evidence_id);
  const title = evidence?.title ?? `Evidence #${source.evidence_id}`;
  const kind = evidence ? SOURCE_KIND_LABELS[evidence.source_kind] ?? evidence.source_kind : "Unknown";
  const authority = evidence
    ? INPUT_AUTHORITY_LABELS[evidence.authority] ?? evidence.authority
    : "Unknown";
  const overlapText = lexicalOverlapText(source.lexical_overlap_score);

  return (
    <li className="rounded-sm border border-border/70 bg-muted/30 p-2 text-xs">
      <p className="font-medium text-foreground">
        {title} <span className="font-normal text-muted-foreground">({kind} · {authority})</span>
      </p>
      <p className="mt-1 text-muted-foreground">Relationship: {source.relationship}</p>
      <blockquote className="mt-1 overflow-x-auto border-l-2 border-border pl-2 font-mono">
        {source.exact_quote}
      </blockquote>
      {overlapText != null && <p className="mt-1 text-muted-foreground">{overlapText}</p>}
    </li>
  );
}

interface ClaimRowProps {
  claim: DraftClaim;
  evidenceById: Map<number, DraftEvidence>;
  onEditDraft(): void;
}

function ClaimRow({ claim, evidenceById, onEditDraft }: ClaimRowProps) {
  const showActions = isUnsupportedOrBlocker(claim);
  const isOpinion = claim.status === "opinion";

  return (
    <li className="rounded-sm border border-border p-4">
      <div className="flex flex-wrap items-center gap-2">
        {isOpinion && <span className="sr-only">Opinion: </span>}
        <Badge variant={claim.status === "supported" ? "default" : "outline"}>
          {CLAIM_STATUS_LABELS[claim.status] ?? claim.status}
        </Badge>
        {(claim.severity === "warning" || claim.severity === "blocker") && (
          <Badge variant="outline" className={SEVERITY_BADGE_CLASS[claim.severity]}>
            {claim.severity === "blocker" ? (
              <XCircle className="mr-1 h-3 w-3" aria-hidden="true" />
            ) : (
              <AlertTriangle className="mr-1 h-3 w-3" aria-hidden="true" />
            )}
            {claim.severity === "blocker" ? "Blocker" : "Warning"}
          </Badge>
        )}
      </div>

      {claim.status === "supported" && (
        <p className="mt-1 text-xs font-medium text-success">{SUPPORTED_BY_EVIDENCE}</p>
      )}

      <p className="mt-2 text-sm text-foreground">{claim.claim_text}</p>
      <p className="mt-1 text-xs text-muted-foreground">{claim.rationale}</p>

      {claim.sources.length > 0 && (
        <ul className="mt-3 space-y-2">
          {claim.sources.map((source) => (
            <ClaimSourceRow key={source.id} source={source} evidenceById={evidenceById} />
          ))}
        </ul>
      )}

      {showActions && (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <Button type="button" size="sm" variant="outline" onClick={onEditDraft}>
            Edit draft
          </Button>
          <details className="text-xs">
            <summary className="cursor-pointer text-muted-foreground">View retrieval audit</summary>
            <pre className="mt-1 max-h-64 overflow-auto rounded-sm bg-muted p-2">
              {safeJsonStringify(claim.retrieval_audit)}
            </pre>
          </details>
        </div>
      )}
    </li>
  );
}

export function DraftClaimsPanel({ draftId, revisionId }: DraftClaimsPanelProps) {
  const statusFilter = useDraftRoomUiStore((s) => s.claimStatusFilter) as DraftClaimStatus | null;
  const setStatusFilter = useDraftRoomUiStore((s) => s.setClaimStatusFilter);
  const setWorkspaceTab = useDraftRoomUiStore((s) => s.setWorkspaceTab);

  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(DEFAULT_PER_PAGE);

  useEffect(() => {
    setPage(1);
  }, [statusFilter, revisionId]);

  const claimsParams = {
    status: statusFilter ?? undefined,
    revision_id: revisionId ?? undefined,
    page,
    per_page: perPage,
  };

  const claimsQuery = useQuery({
    queryKey: draftRoomKeys.claims(draftId, claimsParams),
    queryFn: () => listDraftClaims(draftId, claimsParams),
  });

  // Resolve the job that produced `revisionId`'s claims (via its lightweight
  // DraftRevisionSummary — no manuscript content) so the evidence lookup
  // below can be scoped to that job instead of the draft's entire history.
  const revisionsQuery = useQuery({
    queryKey: draftRoomKeys.revisions(draftId, { per_page: REVISION_LOOKUP_PAGE_SIZE }),
    queryFn: () => listDraftRevisions(draftId, { per_page: REVISION_LOOKUP_PAGE_SIZE }),
    enabled: revisionId != null,
  });
  const waitingForRevisionJob = revisionId != null && revisionsQuery.isPending;
  const scopedJobId =
    revisionId != null
      ? (revisionsQuery.data?.items.find((revision) => revision.id === revisionId)?.job_id ?? undefined)
      : undefined;

  // Best-effort evidence lookup for source title/kind/authority — the claims
  // endpoint only returns `evidence_id` on each source. Pages through every
  // evidence row for the resolved job (bounded — see
  // `fetchEvidenceForLookup`); `evidenceLookupTruncated` below drives an
  // honest note rather than silently mislabeling sources as "Unknown".
  const evidenceLookupQuery = useQuery({
    queryKey: [...draftRoomKeys.evidence(draftId, { job_id: scopedJobId ?? null }), "claims-lookup"] as const,
    queryFn: () => fetchEvidenceForLookup(draftId, scopedJobId, MAX_EVIDENCE_LOOKUP_PAGES),
    enabled: !waitingForRevisionJob,
  });

  const evidenceById = useMemo(() => {
    const map = new Map<number, DraftEvidence>();
    for (const evidence of evidenceLookupQuery.data?.items ?? []) {
      map.set(evidence.id, evidence);
    }
    return map;
  }, [evidenceLookupQuery.data]);
  const evidenceLookupTruncated = evidenceLookupQuery.data?.truncated === true;

  const items = claimsQuery.data?.items ?? [];
  const total = claimsQuery.data?.total ?? 0;
  const opinions = items.filter((claim) => claim.status === "opinion");
  const factual = items.filter((claim) => claim.status !== "opinion");
  const handleEditDraft = () => setWorkspaceTab("draft");

  return (
    <div className="space-y-4">
      <div>
        <span className="mb-1 block text-xs font-medium text-muted-foreground">Status</span>
        <div role="group" aria-label="Filter claims by status" className="flex flex-wrap gap-1.5">
          {[{ value: null, label: "All" } as const, ...DRAFT_CLAIM_STATUSES.map((status) => ({
            value: status,
            label: CLAIM_STATUS_LABELS[status] ?? status,
          }))].map((option) => {
            const isActive = option.value === statusFilter;
            return (
              <button
                key={option.label}
                type="button"
                aria-pressed={isActive}
                onClick={() => setStatusFilter(option.value)}
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

      <Separator />

      {claimsQuery.isPending && <p className="text-sm text-muted-foreground">Loading claims…</p>}
      {claimsQuery.isError && (
        <Alert variant="destructive">
          <AlertTitle>Could not load claims</AlertTitle>
          <AlertDescription>{parseDraftRoomError(claimsQuery.error).detail}</AlertDescription>
        </Alert>
      )}
      {claimsQuery.isSuccess && items.length === 0 && (
        <p className="text-sm text-muted-foreground">No claims match the current filters.</p>
      )}

      {items.length > 0 && evidenceLookupTruncated && (
        <Alert variant="warning">
          <AlertTitle>Some source details could not be resolved</AlertTitle>
          <AlertDescription>
            This draft has more evidence than could be checked. Some claim sources below show only an
            evidence identifier instead of the source&apos;s title, kind, and authority.
          </AlertDescription>
        </Alert>
      )}

      {factual.length > 0 && (
        <section aria-labelledby="draft-claims-factual-heading">
          <h3 id="draft-claims-factual-heading" className="mb-2 text-sm font-semibold text-foreground">
            Factual and quote claims
          </h3>
          <ul className="space-y-3">
            {factual.map((claim) => (
              <ClaimRow key={claim.id} claim={claim} evidenceById={evidenceById} onEditDraft={handleEditDraft} />
            ))}
          </ul>
        </section>
      )}

      {opinions.length > 0 && (
        <section aria-labelledby="draft-claims-opinions-heading">
          <h3 id="draft-claims-opinions-heading" className="mb-2 text-sm font-semibold text-foreground">
            Opinions
          </h3>
          <p className="mb-2 text-xs text-muted-foreground">
            Opinions are not fact-checked claims; they are kept separate from supported/unsupported findings.
          </p>
          <ul className="space-y-3">
            {opinions.map((claim) => (
              <ClaimRow key={claim.id} claim={claim} evidenceById={evidenceById} onEditDraft={handleEditDraft} />
            ))}
          </ul>
        </section>
      )}

      {total > 0 && (
        <Pagination
          page={page}
          limit={perPage}
          total={total}
          onPageChange={setPage}
          onLimitChange={setPerPage}
          isLoading={claimsQuery.isFetching}
          itemName="claims"
        />
      )}
    </div>
  );
}
