import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
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
import {
  canvasKeys,
  getCanvasVersion,
  type CanvasVersionSummary,
} from "@/lib/api/canvas";
import { cn } from "@/lib/utils";

import { CANVAS_DIFF_LEGEND, CANVAS_DIFF_MARKERS, CANVAS_ORIGIN_LABELS } from "./labels";

export interface CanvasCompareProps {
  artifactUid: string;
  versions: CanvasVersionSummary[];
}

type DiffRowKind = "added" | "removed" | "unchanged";

interface DiffRow {
  key: string;
  kind: DiffRowKind;
  marker: string;
  text: string;
}

/** Splits a jsdiff line-change value back into individual lines, dropping the
 * empty trailing element `split("\n")` introduces when the value itself ends
 * in a newline (DraftRevisionDiff pattern). */
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
    const marker = CANVAS_DIFF_MARKERS[kind];
    splitChangeLines(change.value).forEach((line, lineIndex) => {
      rows.push({ key: `${changeIndex}-${lineIndex}`, kind, marker, text: line });
    });
  });
  return rows;
}

function versionOptionLabel(version: CanvasVersionSummary): string {
  const origin = CANVAS_ORIGIN_LABELS[version.origin] ?? version.origin;
  const timestamp = new Date(version.created_at).toLocaleString();
  return `Version ${version.version_no}${version.name ? ` — ${version.name}` : ""} — ${origin} — ${timestamp}`;
}

/** Read-only line diff between any two versions. Add/remove is never signalled
 * by colour alone: every row carries a gutter glyph plus a visually-hidden
 * legend prefix for screen readers (DraftRevisionDiff pattern). */
export function CanvasCompare({ artifactUid, versions }: CanvasCompareProps) {
  const [fromNo, setFromNo] = useState<number | null>(null);
  const [toNo, setToNo] = useState<number | null>(null);

  useEffect(() => {
    if (versions.length === 0) return;
    if (fromNo == null) setFromNo(versions[0].version_no);
    if (toNo == null) setToNo(versions[versions.length - 1].version_no);
  }, [versions, fromNo, toNo]);

  const fromQuery = useQuery({
    queryKey: canvasKeys.version(artifactUid, fromNo ?? -1),
    queryFn: () => getCanvasVersion(artifactUid, fromNo as number),
    enabled: fromNo != null,
    retry: false,
  });
  const toQuery = useQuery({
    queryKey: canvasKeys.version(artifactUid, toNo ?? -1),
    queryFn: () => getCanvasVersion(artifactUid, toNo as number),
    enabled: toNo != null,
    retry: false,
  });

  const rows = useMemo(() => {
    const fromContent = fromQuery.data?.version.content;
    const toContent = toQuery.data?.version.content;
    if (fromContent == null || toContent == null) return null;
    return buildDiffRows(fromContent, toContent);
  }, [fromQuery.data, toQuery.data]);

  const hasChanges = rows != null && rows.some((row) => row.kind !== "unchanged");
  const loading = (fromNo != null && fromQuery.isLoading) || (toNo != null && toQuery.isLoading);

  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <Label htmlFor="canvas-compare-from">Compare from</Label>
          <Select
            value={fromNo != null ? String(fromNo) : undefined}
            onValueChange={(next) => setFromNo(Number(next))}
          >
            <SelectTrigger id="canvas-compare-from" aria-label="Compare from version">
              <SelectValue placeholder="Select a version" />
            </SelectTrigger>
            <SelectContent>
              {versions.map((version) => (
                <SelectItem key={version.version_no} value={String(version.version_no)}>
                  {versionOptionLabel(version)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div>
          <Label htmlFor="canvas-compare-to">Compare to</Label>
          <Select
            value={toNo != null ? String(toNo) : undefined}
            onValueChange={(next) => setToNo(Number(next))}
          >
            <SelectTrigger id="canvas-compare-to" aria-label="Compare to version">
              <SelectValue placeholder="Select a version" />
            </SelectTrigger>
            <SelectContent>
              {versions.map((version) => (
                <SelectItem key={version.version_no} value={String(version.version_no)}>
                  {versionOptionLabel(version)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>

      <div className="flex flex-wrap gap-4 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1">
          <span aria-hidden="true">{CANVAS_DIFF_MARKERS.added}</span>
          <span>{CANVAS_DIFF_LEGEND.added}</span>
        </span>
        <span className="inline-flex items-center gap-1">
          <span aria-hidden="true">{CANVAS_DIFF_MARKERS.removed}</span>
          <span>{CANVAS_DIFF_LEGEND.removed}</span>
        </span>
        <span className="inline-flex items-center gap-1">
          <span aria-hidden="true">{CANVAS_DIFF_MARKERS.unchanged}</span>
          <span>{CANVAS_DIFF_LEGEND.unchanged}</span>
        </span>
      </div>

      {loading ? (
        <div className="flex flex-col gap-1.5" data-testid="canvas-diff-skeleton">
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-4 w-3/4" />
        </div>
      ) : rows == null ? (
        <p className="text-sm text-muted-foreground">
          Select both a From and To version to see differences.
        </p>
      ) : !hasChanges ? (
        <p className="text-sm text-muted-foreground">No differences between these versions.</p>
      ) : (
        <div
          className="max-w-[85ch] overflow-x-auto rounded-sm border border-border"
          data-testid="canvas-diff-rows"
        >
          <div className="font-mono text-xs leading-relaxed">
            {rows.map((row) => (
              <div
                key={row.key}
                data-testid="canvas-diff-row"
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
                <span className="sr-only">{CANVAS_DIFF_LEGEND[row.kind]}: </span>
                <span>{row.text.length > 0 ? row.text : " "}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
