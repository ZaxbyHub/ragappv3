import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Pagination } from "@/components/ui/pagination";
import {
  draftRoomKeys,
  listDraftEvidence,
  parseDraftRoomError,
  type DraftEvidence,
} from "@/lib/api/draftRoom";
import { INPUT_AUTHORITY_LABELS, SOURCE_DELETED_WARNING } from "@/components/draft-room/labels";
import { cn } from "@/lib/utils";

export interface DraftEvidencePanelProps {
  draftId: number;
  jobId: number | null;
}

const DEFAULT_PER_PAGE = 20;

const SOURCE_KIND_LABELS: Record<string, string> = {
  draft_input: "Project input",
  document: "Vault document",
  wiki: "Wiki",
  kms: "Knowledge base",
};

function EvidenceCard({ evidence }: { evidence: DraftEvidence }) {
  const kindLabel = SOURCE_KIND_LABELS[evidence.source_kind] ?? evidence.source_kind;
  const authorityLabel = INPUT_AUTHORITY_LABELS[evidence.authority] ?? evidence.authority;

  return (
    <li
      className={cn(
        "rounded-sm border border-border p-4",
        evidence.source_deleted && "opacity-70"
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="secondary">[{evidence.label}]</Badge>
        <Badge variant="outline">{kindLabel}</Badge>
        <Badge variant="outline">{authorityLabel}</Badge>
        {evidence.source_deleted && (
          <Badge variant="outline" className="border-destructive/50 bg-destructive/10 text-destructive">
            <AlertTriangle className="mr-1 h-3 w-3" aria-hidden="true" />
            Not reusable
          </Badge>
        )}
      </div>

      <p className="mt-2 text-sm font-medium text-foreground">{evidence.title}</p>

      <dl className="mt-1 grid grid-cols-2 gap-x-4 gap-y-0.5 text-xs text-muted-foreground">
        <dt>As of date</dt>
        <dd>{evidence.as_of_date ?? "Unknown"}</dd>
        <dt>Source last updated</dt>
        <dd>{evidence.source_updated_at ?? "Unknown"}</dd>
      </dl>

      <blockquote className="mt-2 overflow-x-auto rounded-sm bg-muted/50 p-2 text-xs font-mono">
        {evidence.passage}
      </blockquote>

      {evidence.source_deleted && (
        <Alert variant="destructive" className="mt-3">
          <AlertTitle>{SOURCE_DELETED_WARNING}</AlertTitle>
          <AlertDescription>This passage can no longer be reused as evidence.</AlertDescription>
        </Alert>
      )}
    </li>
  );
}

export function DraftEvidencePanel({ draftId, jobId }: DraftEvidencePanelProps) {
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(DEFAULT_PER_PAGE);

  useEffect(() => {
    setPage(1);
  }, [jobId]);

  const evidenceParams = { page, per_page: perPage, job_id: jobId ?? undefined };

  const evidenceQuery = useQuery({
    queryKey: draftRoomKeys.evidence(draftId, evidenceParams),
    queryFn: () => listDraftEvidence(draftId, evidenceParams),
    enabled: jobId != null,
  });

  if (jobId == null) {
    return <p className="text-sm text-muted-foreground">No newsroom run has produced evidence yet.</p>;
  }

  const items = evidenceQuery.data?.items ?? [];
  const total = evidenceQuery.data?.total ?? 0;

  return (
    <div className="space-y-4">
      {evidenceQuery.isPending && <p className="text-sm text-muted-foreground">Loading evidence…</p>}
      {evidenceQuery.isError && (
        <Alert variant="destructive">
          <AlertTitle>Could not load evidence</AlertTitle>
          <AlertDescription>{parseDraftRoomError(evidenceQuery.error).detail}</AlertDescription>
        </Alert>
      )}
      {evidenceQuery.isSuccess && items.length === 0 && (
        <p className="text-sm text-muted-foreground">No evidence was captured for this run.</p>
      )}

      {items.length > 0 && (
        <ul className="space-y-3">
          {items.map((evidence) => (
            <EvidenceCard key={evidence.id} evidence={evidence} />
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
          isLoading={evidenceQuery.isFetching}
          itemName="evidence items"
        />
      )}
    </div>
  );
}
