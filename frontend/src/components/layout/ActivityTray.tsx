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

import { useRef, useState } from "react";
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

/** HTTP status of an action failure, when the error carries one. */
function errorStatus(e: unknown): number | undefined {
  const response = (e as { response?: { status?: number } } | null)?.response;
  return response?.status;
}

export function ActivityTray() {
  const { rows, loading, refresh } = useActivityJobs();
  const [collapsed, setCollapsed] = useState(false);
  const activeCount = rows.filter((r) => !r.terminal).length;
  // Row-keyed in-flight marker: the clicked action's button is disabled
  // until its request settles, so a double-click cannot double-POST
  // (PRR-006 / review F-005c).
  const [pendingKey, setPendingKey] = useState<string | null>(null);
  // Rows whose Cancel/Retry the server refused with 403 (cancel requires
  // per-file vault admin; wiki/draft retry requires vault write): stop
  // offering affordances that can only ever fail for this user (PRR-005 /
  // review F-009).
  const deniedCancelKeysRef = useRef<Set<string>>(new Set());
  const sectionRef = useRef<HTMLElement | null>(null);
  const focusedKeyRef = useRef<string | null>(null);
  const prevRowKeysRef = useRef<Set<string>>(new Set());

  async function runAction(
    row: ActivityJobRow,
    action: () => Promise<unknown>,
    failureLabel: string
  ) {
    setPendingKey(row.key);
    try {
      await action();
    } catch (e) {
      if (errorStatus(e) === 403) {
        // A 403 means this user lacks the server-side gate for that action
        // on that row: stop offering an affordance that can only ever fail.
        deniedCancelKeysRef.current.add(row.key);
      }
      // 403 (vault-admin gate) and 409 (already finished) surface as the
      // server's message; the next poll reconciles the row either way.
      toast.error(e instanceof Error ? e.message : failureLabel);
    } finally {
      setPendingKey(null);
      // Optional-chained because the hook's test doubles return only
      // { rows, loading } (frozen mock contract).
      refresh?.();
    }
  }

  function handleCancel(row: ActivityJobRow) {
    if (row.family === "wiki" && row.jobId != null && row.vaultId != null) {
      return runAction(row, () => cancelWikiJob(row.jobId as number, row.vaultId as number), "Cancel failed");
    }
    if (row.family === "draft-room" && row.draftId != null && row.jobId != null) {
      return runAction(row, () => cancelDraftJob(row.draftId as number, row.jobId as number), "Cancel failed");
    }
    if (row.family === "ingest" && row.fileId != null) {
      return runAction(row, () => cancelDocumentIngest(row.fileId as string), "Cancel failed");
    }
    return Promise.resolve();
  }

  function handleRetry(row: ActivityJobRow) {
    if (row.family === "wiki" && row.jobId != null && row.vaultId != null) {
      return runAction(row, () => retryWikiJob(row.jobId as number, row.vaultId as number), "Retry failed");
    }
    if (row.family === "draft-room" && row.draftId != null && row.jobId != null) {
      return runAction(row, () => retryDraftJob(row.draftId as number, row.jobId as number), "Retry failed");
    }
    return Promise.resolve();
  }

  // Focus guard (PRR-029): a poll tick can unmount the focused row (a
  // successful cancel removes it; the terminal cap ages it out) — restore
  // focus to the tray header instead of dropping it to <body>.
  const prevKeys = prevRowKeysRef.current;
  const currentKeys = new Set(rows.map((r) => r.key));
  if (
    focusedKeyRef.current &&
    !currentKeys.has(focusedKeyRef.current) &&
    prevKeys.has(focusedKeyRef.current) &&
    typeof document !== "undefined" &&
    document.activeElement instanceof HTMLElement &&
    document.activeElement.closest('[aria-label="Activity"]')
  ) {
    sectionRef.current
      ?.querySelector<HTMLButtonElement>("button[aria-expanded]")
      ?.focus();
  }
  prevRowKeysRef.current = currentKeys;

  return (
    // Stacked ABOVE the upload indicator on both breakpoints: the indicator
    // is `bottom-20 right-4 z-50` on mobile and `md:bottom-4` on desktop, so
    // the tray's previous `bottom-20` anchor sat exactly under it and the
    // upload card painted over the tray's only affordance (PRR-027 /
    // review F-002, execution-proven at 375x812).
    // A labelled <section> carries the region landmark role implicitly —
    // AC1's `getByRole("region", { name: /activity/i })` resolves against
    // the implicit role (jsx-a11y forbids the explicit attribute).
    <section
      ref={sectionRef}
      aria-label="Activity"
      className="fixed bottom-40 right-4 z-40 w-80 max-w-[calc(100vw-2rem)] md:bottom-24"
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
        {/* Screen-reader status channel for poll-driven job churn (the
            count itself changes as jobs start/finish; row-level text would
            be too chatty to announce) — PRR-028 / review F-007. */}
        <span className="sr-only" aria-live="polite">
          {activeCount === 0
            ? "No active background jobs"
            : `${activeCount} active background job${activeCount === 1 ? "" : "s"}`}
        </span>
        {!collapsed && (
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
        )}
        {!collapsed && rows.length > 0 && (
          <ul className="max-h-72 overflow-auto px-3 py-2">
            {rows.map((row) => {
              const denied = deniedCancelKeysRef.current.has(row.key);
              const cancellable = row.cancellable && !denied;
              const retryable = row.retryable && !denied;
              const pending = pendingKey === row.key;
              return (
                <li
                  key={row.key}
                  onFocusCapture={() => {
                    focusedKeyRef.current = row.key;
                  }}
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
                      {cancellable && (
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          className="h-7 px-2 text-xs"
                          disabled={pending}
                          onClick={() => {
                            void handleCancel(row);
                          }}
                        >
                          <XCircle className="mr-1 h-3 w-3" aria-hidden="true" />
                          Cancel
                        </Button>
                      )}
                      {retryable && (
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          className="h-7 px-2 text-xs"
                          disabled={pending}
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
              );
            })}
          </ul>
        )}
      </div>
    </section>
  );
}
