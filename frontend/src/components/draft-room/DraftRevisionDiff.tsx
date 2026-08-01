import { useId, useMemo } from "react";
import { diffLines } from "diff";

import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import type { DraftRevisionSummary } from "@/lib/api/draftRoom";
import { cn } from "@/lib/utils";

import { DIFF_LEGEND, DIFF_MARKERS, FACT_STATUS_LABELS } from "./labels";

export interface DraftRevisionDiffProps {
  revisions: DraftRevisionSummary[];
  fromRevisionId: number | null;
  toRevisionId: number | null;
  onSelectFrom(id: number): void;
  onSelectTo(id: number): void;
  fromContent: string | null;
  toContent: string | null;
  loading?: boolean;
}

type DiffRowKind = "added" | "removed" | "unchanged";

interface DiffRow {
  key: string;
  kind: DiffRowKind;
  marker: string;
  text: string;
}

function revisionSourceLabel(source: DraftRevisionSummary["source"]): string {
  return source === "manual" ? "Manual edit" : "Newsroom";
}

function revisionOptionLabel(revision: DraftRevisionSummary): string {
  const factLabel = FACT_STATUS_LABELS[revision.fact_status] ?? revision.fact_status;
  const timestamp = new Date(revision.created_at).toLocaleString();
  return `Revision ${revision.revision_no} — ${revisionSourceLabel(revision.source)} — ${factLabel} — ${timestamp}`;
}

/** Splits a jsdiff line-change value back into individual lines, dropping the
 * empty trailing element `split("\n")` introduces when the value itself ends
 * in a newline (as jsdiff's line chunks normally do). */
function splitChangeLines(value: string): string[] {
  const lines = value.split("\n");
  if (value.endsWith("\n") && lines.length > 0 && lines[lines.length - 1] === "") {
    lines.pop();
  }
  return lines;
}

function buildDiffRows(from: string, to: string): DiffRow[] {
  const changes = diffLines(from, to);
  const rows: DiffRow[] = [];
  changes.forEach((change, changeIndex) => {
    const kind: DiffRowKind = change.added ? "added" : change.removed ? "removed" : "unchanged";
    const marker = DIFF_MARKERS[kind];
    splitChangeLines(change.value).forEach((line, lineIndex) => {
      rows.push({ key: `${changeIndex}-${lineIndex}`, kind, marker, text: line });
    });
  });
  return rows;
}

/** Read-only line diff between two revisions. Add/remove is never signalled
 * by colour alone: every row carries the `DIFF_MARKERS` glyph in the gutter
 * plus a visually-hidden `DIFF_LEGEND` prefix for screen readers. */
export function DraftRevisionDiff({
  revisions,
  fromRevisionId,
  toRevisionId,
  onSelectFrom,
  onSelectTo,
  fromContent,
  toContent,
  loading,
}: DraftRevisionDiffProps) {
  const fromSelectId = useId();
  const toSelectId = useId();

  const rows = useMemo(() => {
    if (fromContent == null || toContent == null) return null;
    return buildDiffRows(fromContent, toContent);
  }, [fromContent, toContent]);

  const hasChanges = rows != null && rows.some((row) => row.kind !== "unchanged");

  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <Label htmlFor={fromSelectId}>Compare from</Label>
          <Select
            value={fromRevisionId != null ? String(fromRevisionId) : undefined}
            onValueChange={(next) => onSelectFrom(Number(next))}
          >
            <SelectTrigger id={fromSelectId}>
              <SelectValue placeholder="Select a revision" />
            </SelectTrigger>
            <SelectContent>
              {revisions.map((revision) => (
                <SelectItem key={revision.id} value={String(revision.id)}>
                  {revisionOptionLabel(revision)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div>
          <Label htmlFor={toSelectId}>Compare to</Label>
          <Select
            value={toRevisionId != null ? String(toRevisionId) : undefined}
            onValueChange={(next) => onSelectTo(Number(next))}
          >
            <SelectTrigger id={toSelectId}>
              <SelectValue placeholder="Select a revision" />
            </SelectTrigger>
            <SelectContent>
              {revisions.map((revision) => (
                <SelectItem key={revision.id} value={String(revision.id)}>
                  {revisionOptionLabel(revision)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>

      <div className="flex flex-wrap gap-4 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1">
          <span aria-hidden="true">{DIFF_MARKERS.added}</span>
          <span>{DIFF_LEGEND.added}</span>
        </span>
        <span className="inline-flex items-center gap-1">
          <span aria-hidden="true">{DIFF_MARKERS.removed}</span>
          <span>{DIFF_LEGEND.removed}</span>
        </span>
        <span className="inline-flex items-center gap-1">
          <span aria-hidden="true">{DIFF_MARKERS.unchanged}</span>
          <span>{DIFF_LEGEND.unchanged}</span>
        </span>
      </div>

      {loading ? (
        <div className="flex flex-col gap-1.5" data-testid="draft-diff-skeleton">
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-4 w-3/4" />
          <Skeleton className="h-4 w-5/6" />
        </div>
      ) : rows == null ? (
        <p className="text-sm text-muted-foreground">
          Select both a From and To revision to see differences.
        </p>
      ) : !hasChanges ? (
        <p className="text-sm text-muted-foreground">No differences between these revisions.</p>
      ) : (
        <div className="max-w-[85ch] overflow-x-auto rounded-sm border border-border">
          <div className="font-mono text-xs leading-relaxed">
            {rows.map((row) => (
              <div
                key={row.key}
                data-testid="draft-diff-row"
                data-diff-kind={row.kind}
                className={cn(
                  "flex gap-2 whitespace-pre px-2 py-0.5",
                  row.kind === "added" && "bg-success/10",
                  row.kind === "removed" && "bg-destructive/10"
                )}
              >
                <span aria-hidden="true" className="w-4 shrink-0 select-none text-center">
                  {row.marker}
                </span>
                <span className="sr-only">{DIFF_LEGEND[row.kind]}: </span>
                <span>{row.text.length > 0 ? row.text : " "}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
