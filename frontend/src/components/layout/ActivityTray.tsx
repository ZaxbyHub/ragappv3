// frontend/src/components/layout/ActivityTray.tsx
//
// Shell-level Activity tray (issue #784, [Workstream M] PR 4 of 4): the one
// server-sourced surface listing running and recent background jobs of all
// five families (ingest / wiki / draft-room / kms / reindex), mounted by
// PageShell on every authenticated route. Rows come from useActivityJobs —
// server state, never the client upload store — so a reload or another tab
// loses nothing. Cancel reaches wiki / draft-room / ingest server clients;
// Retry reaches the wiki / draft-room ones. The per-page panels
// (WikiJobsPanel, DraftWorkspace's cancel UI, KMSPage) stay untouched.
//
// Markup contract pinned by the frozen acceptance checks
// (ActivityTray.m04.test.tsx / PageShell.m04.test.tsx): exactly one
// role="region" landmark named "Activity"; rows are listitems visible by
// default; the family renders verbatim ("draft-room", not "Draft Room");
// exactly one phase text element per row (raw status is never also
// rendered); row keys never appear as DOM text; buttons are named plain
// "Cancel" / "Retry".

import { useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { useActivityJobs } from "@/hooks/useJobStatus";
import type { ActivityJobRow } from "@/hooks/useJobStatus";
import { cancelDocumentIngest } from "@/lib/api";
import { cancelDraftJob, retryDraftJob } from "@/lib/api/draftRoom";
import { cancelWikiJob, retryWikiJob } from "@/lib/api/wiki";
import { Activity, ChevronDown, Loader2, RotateCcw, XCircle } from "lucide-react";

function rowPhaseTone(row: ActivityJobRow): string {
  if (row.status === "failed") {
    return "text-destructive";
  }
  return "text-muted-foreground";
}

export function ActivityTray() {
  const { rows, loading, refresh } = useActivityJobs();
  const [collapsed, setCollapsed] = useState(false);
  const activeCount = rows.filter((r) => !r.terminal).length;

  async function handleCancel(row: ActivityJobRow) {
    try {
      if (row.family === "wiki" && row.jobId != null && row.vaultId != null) {
        await cancelWikiJob(row.jobId, row.vaultId);
      } else if (row.family === "draft-room" && row.draftId != null && row.jobId != null) {
        await cancelDraftJob(row.draftId, row.jobId);
      } else if (row.family === "ingest" && row.fileId != null) {
        await cancelDocumentIngest(row.fileId);
      }
    } catch (e) {
      // 403 (vault-admin gate) and 409 (already finished) surface as the
      // server's message; the next poll reconciles the row either way.
      toast.error(e instanceof Error ? e.message : "Cancel failed");
    } finally {
      // Optional-chained because the hook's test doubles return only
      // { rows, loading } (frozen mock contract).
      refresh?.();
    }
  }

  async function handleRetry(row: ActivityJobRow) {
    try {
      if (row.family === "wiki" && row.jobId != null && row.vaultId != null) {
        await retryWikiJob(row.jobId, row.vaultId);
      } else if (row.family === "draft-room" && row.draftId != null && row.jobId != null) {
        await retryDraftJob(row.draftId, row.jobId);
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Retry failed");
    } finally {
      refresh?.();
    }
  }

  return (
    // A labelled <section> carries the region landmark role implicitly —
    // AC1's `getByRole("region", { name: /activity/i })` resolves against
    // the implicit role (jsx-a11y forbids the explicit attribute).
    <section
      aria-label="Activity"
      className="fixed bottom-20 right-4 z-40 w-80 max-w-[calc(100vw-2rem)]"
    >
      <div className="rounded-lg border bg-background/95 shadow-lg backdrop-blur">
        <button
          type="button"
          aria-expanded={!collapsed}
          onClick={() => {
            // Expand-only by design: an automated helper (or a user) can
            // click "Activity" while the first poll is still in flight, and
            // that in-flight click must never hide the rows that land a
            // moment later (observed live against the built app — the
            // decision-time-empty click raced the first fetch). Collapsing
            // is owned by the Hide button inside the expanded list.
            setCollapsed(false);
          }}
          className="flex w-full items-center gap-2 px-3 py-2 text-sm font-medium text-foreground"
        >
          {loading ? (
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          ) : (
            <Activity className="h-4 w-4" aria-hidden="true" />
          )}
          Activity{activeCount > 0 ? ` (${activeCount})` : ""}
        </button>
        {!collapsed && rows.length > 0 && (
          <>
            <div className="flex items-center justify-end border-t px-3 py-1">
              <button
                type="button"
                onClick={() => setCollapsed(true)}
                className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
              >
                <ChevronDown className="h-3 w-3" aria-hidden="true" />
                Hide
              </button>
            </div>
            <ul className="max-h-72 overflow-auto px-3 py-2">
            {rows.map((row) => (
              <li
                key={row.key}
                className="flex flex-col gap-1 border-b py-2 last:border-b-0"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="text-xs font-medium uppercase tracking-wide text-foreground">
                    {row.family}
                  </span>
                  <span className={`text-xs ${rowPhaseTone(row)}`}>{row.phase}</span>
                </div>
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-xs text-foreground" title={row.title}>
                    {row.title}
                  </span>
                  <span className="flex shrink-0 items-center gap-1">
                    {row.cancellable && (
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className="h-7 px-2 text-xs"
                        onClick={() => {
                          void handleCancel(row);
                        }}
                      >
                        <XCircle className="mr-1 h-3 w-3" aria-hidden="true" />
                        Cancel
                      </Button>
                    )}
                    {row.retryable && (
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className="h-7 px-2 text-xs"
                        onClick={() => {
                          void handleRetry(row);
                        }}
                      >
                        <RotateCcw className="mr-1 h-3 w-3" aria-hidden="true" />
                        Retry
                      </Button>
                    )}
                  </span>
                </div>
              </li>
            ))}
            </ul>
          </>
        )}
      </div>
    </section>
  );
}
