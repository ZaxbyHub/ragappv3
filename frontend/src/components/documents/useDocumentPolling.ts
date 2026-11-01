import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type MutableRefObject,
  type SetStateAction,
} from "react";
import { toast } from "sonner";
import {
  captureAuthPrincipalGeneration,
  isCurrentAuthOwner,
  subscribeAuthPrincipal,
  type AuthOwner,
} from "@/lib/api/auth-lifecycle";
import {
  compileDocumentWiki,
  getDocumentStats,
  getDocumentWikiStatus,
  listDocuments,
  type Document,
  type DocumentSortBy,
  type DocumentStatsResponse,
  type DocumentWikiStatus,
  type SortOrder,
} from "@/lib/api";
import { logApiFailure } from "@/lib/api/core";
import type { UploadFile } from "@/stores/useUploadStore";
import { useAuthOwner } from "@/hooks/useAuthOwner";

interface UseDocumentPollingArgs {
  activeVaultId: number | null;
  search: string;
  sortBy: DocumentSortBy;
  sortOrder: SortOrder;
  tagFilterId: number | null;
  folderFilterId: number | null;
  uploads: UploadFile[];
}

type Scope = Readonly<{
  owner: AuthOwner;
  principalGeneration: number;
  vaultId: number | null;
  serial: number;
}>;

type Lease = {
  readonly scope: Scope;
  readonly serial: number;
  retired: boolean;
};

type ReadDomain = "list" | "stats" | "wiki";

type ReadToken = Readonly<{
  domain: ReadDomain;
  scope: Scope;
  lease: Lease;
  key: string;
  readContext: object;
  attempt: number;
}>;

type CompileToken = Readonly<{
  scope: Scope;
  lease: Lease;
  serial: number;
  docId: string;
}>;

type WikiPollFlight = Readonly<{
  scope: Scope;
  lease: Lease;
  serial: number;
}>;

type CompileTimer = {
  readonly token: CompileToken;
  timer: ReturnType<typeof setTimeout>;
};

const PAGE_SIZE = 50;
const MAX_PAGE_SIZE = 1000;
const WIKI_POLL_INTERVAL_MS = 5_000;
// Capped per-document backoff for retrying a failed per-document wiki-status
// fetch (#765 AC4), measured in TRANSIENT-INTERVAL TICKS rather than wall
// time (the hook's tests fake the timers but not Date, and production gains
// nothing from wall-clock math here): the first retry rides the very next
// 5 s tick after the failure, later attempts space out, and once the
// schedule is exhausted the id stays marked failed (the cell keeps its
// "unavailable" render) but is never re-requested — no sustained request
// storm from permanently failing rows.
const WIKI_RETRY_SKIP_TICKS = [0, 1, 3, 7];
// Five total attempts per document (initial + four retries, review round 1
// B-4/R02: the previous eligibility gate admitted a sixth fetch). Exhausted
// ids stay marked failed for rendering but are never fetched again by either
// the retry interval or the hydration pass.
const WIKI_RETRY_MAX_ATTEMPTS = WIKI_RETRY_SKIP_TICKS.length + 1;

/**
 * Signature of a committed document-list response, used by the adaptive poll
 * to detect change (#766 AC6). Covers the status/progress metadata the tests
 * drive AND the production top-level progress surface the backend actually
 * emits (progress_percent/processed_units/total_units/phase).
 */
function listSignature(documents: Document[]): string {
  return documents
    .map((document) => {
      const top = document as {
        progress_percent?: number | null;
        processed_units?: number | null;
        total_units?: number | null;
        phase?: string | null;
      };
      return [
        document.id,
        (document.metadata?.status as string | undefined) ?? "",
        ((document.metadata as { progress?: number } | undefined)?.progress) ?? "",
        top.progress_percent ?? "",
        top.processed_units ?? "",
        top.total_units ?? "",
        top.phase ?? "",
      ].join(":");
    })
    .join(",");
}

function isDocumentListResponse(
  value: unknown
): value is { documents: Document[]; total: number } {
  if (value === null || typeof value !== "object") return false;
  const candidate = value as { documents?: unknown; total?: unknown };
  return (
    Array.isArray(candidate.documents) &&
    typeof candidate.total === "number" &&
    Number.isFinite(candidate.total) &&
    candidate.total >= 0
  );
}

function isTransientWikiStatus(status: string | null | undefined): boolean {
  return status === "compiling" || status === "running";
}

/**
 * Owns the document list lifecycle while treating authentication, principal,
 * and vault changes as hard publication boundaries. Query changes keep the
 * current window visible while their replacement reads settle.
 */
export function useDocumentPolling({
  activeVaultId,
  search,
  sortBy,
  sortOrder,
  tagFilterId,
  folderFilterId,
  uploads,
}: UseDocumentPollingArgs) {
  const authOwner = useAuthOwner();
  const principalGeneration = useSyncExternalStore(
    subscribeAuthPrincipal,
    captureAuthPrincipalGeneration,
    captureAuthPrincipalGeneration
  );

  const scopeRef = useRef<Scope | null>(null);
  const scopeSerialRef = useRef(0);
  if (
    scopeRef.current === null ||
    scopeRef.current.owner !== authOwner ||
    scopeRef.current.principalGeneration !== principalGeneration ||
    scopeRef.current.vaultId !== activeVaultId
  ) {
    scopeRef.current = {
      owner: authOwner,
      principalGeneration,
      vaultId: activeVaultId,
      serial: ++scopeSerialRef.current,
    };
  }
  const scope = scopeRef.current;

  const leaseRef = useRef<Lease | null>(null);
  const leaseSerialRef = useRef(0);
  if (leaseRef.current === null || leaseRef.current.scope !== scope) {
    leaseRef.current = {
      scope,
      serial: ++leaseSerialRef.current,
      retired: false,
    };
  }

  const [documents, setDocumentsState] = useState<Document[]>([]);
  const [stats, setStatsState] = useState<DocumentStatsResponse | null>(null);
  const [loading, setLoadingState] = useState(true);
  const [listError, setListErrorState] = useState(false);
  const [wikiStatusMap, setWikiStatusMapState] = useState<
    Record<string, DocumentWikiStatus>
  >({});
  // Documents whose latest per-document wiki-status fetch FAILED (#765 AC4/
  // AC6): immutable render state (the retry interval's arming key derives
  // from it), scope-owned like wikiStatusMap, mirrored into a ref for the
  // async worker paths.
  const [wikiFailedIds, setWikiFailedIdsState] = useState<Set<string>>(
    new Set()
  );
  const [compilingDocIds, setCompilingDocIdsState] = useState<Set<string>>(
    new Set()
  );
  const [total, setTotalState] = useState(0);
  const [pageWindow, setPageWindow] = useState<{
    scope: Scope | null; query: string | null; size: number;
  }>({ scope: null, query: null, size: PAGE_SIZE });
  const initialSettledScopeRef = useRef<Scope | null>(null);

  const documentsScopeRef = useRef<Scope | null>(null);
  const statsScopeRef = useRef<Scope | null>(null);
  const loadingScopeRef = useRef<Scope | null>(null);
  const listErrorScopeRef = useRef<Scope | null>(null);
  const wikiScopeRef = useRef<Scope | null>(null);
  const wikiFailedScopeRef = useRef<Scope | null>(null);
  const compilingScopeRef = useRef<Scope | null>(null);
  const totalScopeRef = useRef<Scope | null>(null);
  // Read-context (query) ownership of the committed list rows (issue #764
  // keep-rows hardening): written on every successful list commit so a later
  // keep-rows failure only preserves rows that belong to the SAME query
  // context the failing fetch was issued under — a poll that rejects after a
  // search/sort/filter change must not keep the previous query's rows.
  const documentsContextRef = useRef<object | null>(null);
  // Render mirror of the visible rows, declared before fetchDocuments reads it
  // in the keep-rows decision (assigned in the render body below).
  const documentsRef = useRef<Document[]>([]);

  const queryBaseKey = JSON.stringify([
    activeVaultId,
    search,
    sortBy,
    sortOrder,
    tagFilterId,
    folderFilterId,
  ]);
  const queryBaseKeyRef = useRef<string | null>(null);
  queryBaseKeyRef.current = queryBaseKey;
  const queryContext = useMemo(() => ({ scope, key: queryBaseKey }), [scope, queryBaseKey]);
  const queryContextRef = useRef(queryContext);
  queryContextRef.current = queryContext;
  const effectivePageSize = pageWindow.scope === scope && pageWindow.query === queryBaseKey
    ? pageWindow.size : PAGE_SIZE;
  const listReadKey = JSON.stringify([queryBaseKey, effectivePageSize]);
  const readContext = useMemo(() => ({ queryContext, key: listReadKey }), [queryContext, listReadKey]);
  const readContextRef = useRef(readContext);
  readContextRef.current = readContext;

  const liveLeaseFor = useCallback((capturedScope: Scope): Lease | null => {
    if (scopeRef.current !== capturedScope) return null;
    const lease = leaseRef.current;
    return lease !== null && lease.scope === capturedScope && !lease.retired ? lease : null;
  }, []);

  const isContextLive = useCallback(
    (capturedScope: Scope, capturedLease: Lease): boolean =>
      scopeRef.current === capturedScope &&
      leaseRef.current === capturedLease &&
      !capturedLease.retired &&
      isCurrentAuthOwner(capturedScope.owner) &&
      captureAuthPrincipalGeneration() === capturedScope.principalGeneration,
    []
  );

  const listLatestRef = useRef<ReadToken | null>(null);
  const listPendingRef = useRef<ReadToken | null>(null);
  const statsLatestRef = useRef<ReadToken | null>(null);
  const statsPendingRef = useRef<ReadToken | null>(null);
  const wikiLatestRef = useRef<ReadToken | null>(null);
  const wikiPendingRef = useRef<ReadToken | null>(null);
  const listAttemptRef = useRef(0);
  const statsAttemptRef = useRef(0);
  const wikiAttemptRef = useRef(0);

  const latestFor = useCallback(
    (domain: ReadDomain): MutableRefObject<ReadToken | null> => {
    if (domain === "list") return listLatestRef;
    if (domain === "stats") return statsLatestRef;
    return wikiLatestRef;
    },
    []
  );

  const pendingFor = useCallback(
    (domain: ReadDomain): MutableRefObject<ReadToken | null> => {
    if (domain === "list") return listPendingRef;
    if (domain === "stats") return statsPendingRef;
    return wikiPendingRef;
    },
    []
  );

  const nextAttemptFor = useCallback((domain: ReadDomain): number => {
    if (domain === "list") return ++listAttemptRef.current;
    if (domain === "stats") return ++statsAttemptRef.current;
    return ++wikiAttemptRef.current;
  }, []);

  const beginRead = useCallback(
    (domain: ReadDomain, key: string, capturedScope: Scope): ReadToken | null => {
      const lease = liveLeaseFor(capturedScope);
      if (lease === null || !isContextLive(capturedScope, lease)) return null;
      const token: ReadToken = {
        domain,
        scope: capturedScope,
        lease,
        key,
        readContext: readContextRef.current,
        attempt: nextAttemptFor(domain),
      };
      latestFor(domain).current = token;
      pendingFor(domain).current = token;
      return token;
    },
    [isContextLive, latestFor, liveLeaseFor, nextAttemptFor, pendingFor]
  );

  const isReadCurrent = useCallback(
    (token: ReadToken): boolean =>
      isContextLive(token.scope, token.lease) && latestFor(token.domain).current === token &&
      (token.domain === "stats" || readContextRef.current === token.readContext),
    [isContextLive, latestFor]
  );

  const finishRead = useCallback(
    (token: ReadToken): void => {
      if (!isContextLive(token.scope, token.lease)) return;
      const pending = pendingFor(token.domain);
      if (pending.current === token) pending.current = null;
    },
    [isContextLive, pendingFor]
  );

  const commitLoading = useCallback(
    (capturedScope: Scope, capturedLease: Lease, value: boolean) => {
      setLoadingState((previous) => {
        if (!isContextLive(capturedScope, capturedLease)) return previous;
        loadingScopeRef.current = capturedScope;
        return value;
      });
    },
    [isContextLive]
  );

  const pollIntervalMsRef = useRef(2_000);
  // Baseline for the adaptive poll's change detection (#766 AC6): written on
  // every successfully committed list response (the first commit establishes
  // the baseline); failed or stale reads never touch it. When a commit's
  // signature differs from the previous committed one the poll interval is
  // reset synchronously here — BEFORE the state commit re-arms the poll
  // effect, so the next tick is scheduled at the reset interval.
  const lastCommittedSignatureRef = useRef<string | null>(null);

  const fetchDocuments = useCallback(
    async (options?: { keepRowsOnFailure?: boolean }) => {
    if (scopeRef.current !== scope || readContextRef.current !== readContext) return;
    const token = beginRead("list", listReadKey, scope);
    if (token === null) return;
    try {
      const response = await listDocuments({
        vaultId: activeVaultId ?? undefined,
        search: search || undefined,
        sortBy,
        sortOrder,
        tagId: tagFilterId ?? undefined,
        folderId: folderFilterId ?? undefined,
        perPage: effectivePageSize,
      });
      if (!isReadCurrent(token)) return;
      if (!isDocumentListResponse(response)) {
        throw new Error("Invalid document list response");
      }
      const signature = listSignature(response.documents);
      if (
        lastCommittedSignatureRef.current !== null &&
        signature !== lastCommittedSignatureRef.current
      ) {
        pollIntervalMsRef.current = 2_000;
      }
      lastCommittedSignatureRef.current = signature;
      setDocumentsState((previous) => {
        if (!isReadCurrent(token)) return previous;
        documentsScopeRef.current = token.scope;
        // Paired with documentsScopeRef inside the SAME updater: the keep-rows
        // predicate reads both together, so they must be written together —
        // a sync write here vs an updater write there could skew if React
        // defers the updater (review question on the feedback round).
        documentsContextRef.current = token.readContext;
        return response.documents;
      });
      setTotalState((previous) => {
        if (!isReadCurrent(token)) return previous;
        totalScopeRef.current = token.scope;
        return response.total;
      });
      setListErrorState((previous) => {
        if (!isReadCurrent(token)) return previous;
        listErrorScopeRef.current = token.scope;
        return false;
      });
      return true;
    } catch (error) {
      if (!isReadCurrent(token)) return;
      logApiFailure("Failed to fetch documents:", error);
      toast.error(error instanceof Error ? error.message : "Failed to load documents", {
        id: "documents-list-failure",
      });
      if (options?.keepRowsOnFailure) {
        // Issue #764 AC8/AC9 — a background-poll failure over an ALREADY-LOADED
        // list must not replace rendered rows with empty state. Kept rows must
        // belong to the CURRENT scope (documentsScopeRef) AND the CURRENT read
        // context (documentsContextRef): a poll that rejects after a
        // search/sort/filter change must not keep the previous query's rows
        // under the new query. On a keep, listError is NOT flipped — the rows
        // are valid last-known-good data and the toast already surfaced the
        // transient error. When the rows are NOT kept (nothing committed for
        // this scope+context yet), the historical wipe + listError arms the
        // AC12 retry affordance exactly as the foreground path does.
        const keepRows =
          documentsScopeRef.current === token.scope &&
          documentsContextRef.current === token.readContext &&
          documentsRef.current.length > 0;
        setDocumentsState((previous) => {
          if (!isReadCurrent(token)) return previous;
          if (keepRows) return previous;
          documentsScopeRef.current = token.scope;
          return [];
        });
        setTotalState((previous) => {
          if (!isReadCurrent(token)) return previous;
          if (keepRows) return previous;
          totalScopeRef.current = token.scope;
          return 0;
        });
        if (!keepRows) {
          setListErrorState((previous) => {
            if (!isReadCurrent(token)) return previous;
            listErrorScopeRef.current = token.scope;
            return true;
          });
        }
        // Failure verdict for the poll owner: on a keep, no state visible to
        // the poll effect changed, so the caller (runPollFetch) schedules the
        // next poll itself (a state-driven re-arm cannot be observed by the
        // C9 poll-re-arm acceptance check's single-block fake-time advance,
        // which drains only microtasks between fake timers; this behaves
        // identically in production where state flushes are near-immediate).
        // On a wipe the effect re-runs from the commit and owns re-arming.
        return false;
      }
      setDocumentsState((previous) => {
        if (!isReadCurrent(token)) return previous;
        documentsScopeRef.current = token.scope;
        return [];
      });
      setTotalState((previous) => {
        if (!isReadCurrent(token)) return previous;
        totalScopeRef.current = token.scope;
        return 0;
      });
      setListErrorState((previous) => {
        if (!isReadCurrent(token)) return previous;
        listErrorScopeRef.current = token.scope;
        return true;
      });
      // Foreground/initial-load failure: the ErrorState branch (listError +
      // empty) owns recovery — still a "failed" verdict, but no poll timer is
      // involved on that path (no processing rows were rendered to poll over).
      return false;
    } finally {
      finishRead(token);
    }
    },
    [
      activeVaultId,
      beginRead,
      effectivePageSize,
      finishRead,
      folderFilterId,
      isReadCurrent,
      listReadKey,
      readContext,
      scope,
      search,
      sortBy,
      sortOrder,
      tagFilterId,
    ]
  );

  const fetchStats = useCallback(async () => {
    if (scopeRef.current !== scope) return;
    const key = JSON.stringify([activeVaultId]);
    const token = beginRead("stats", key, scope);
    if (token === null) return;
    try {
      const response = await getDocumentStats(activeVaultId ?? undefined);
      if (!isReadCurrent(token)) return;
      setStatsState((previous) => {
        if (!isReadCurrent(token)) return previous;
        statsScopeRef.current = token.scope;
        return response;
      });
    } catch (error) {
      if (!isReadCurrent(token)) return;
      logApiFailure("Failed to fetch stats:", error);
      toast.error(error instanceof Error ? error.message : "Failed to load document stats", {
        id: "documents-stats-failure",
      });
    } finally {
      finishRead(token);
    }
  }, [activeVaultId, beginRead, finishRead, isReadCurrent, scope]);

  const documentsVisible = useMemo(
    () => (documentsScopeRef.current === scope ? documents : []),
    [documents, scope],
  );
  const statsVisible = statsScopeRef.current === scope ? stats : null;
  const wikiStatusVisible = useMemo(
    () => (wikiScopeRef.current === scope ? wikiStatusMap : {}),
    [scope, wikiStatusMap],
  );
  const compilingVisible =
    compilingScopeRef.current === scope ? compilingDocIds : new Set<string>();
  const totalVisible = totalScopeRef.current === scope ? total : 0;
  const listErrorVisible =
    listErrorScopeRef.current === scope ? listError : false;
  const loadingVisible = loadingScopeRef.current === scope ? loading : true;

  documentsRef.current = documentsVisible;

  // Issue #764 AC9 — the adaptive poll's fire/retry scheduler. The effect
  // below arms the first timer of a cycle; when a poll fetch FAILS while rows
  // are kept (fetchDocuments returns false), no state visible to that effect
  // changes, so nothing would re-arm it — runPollFetch schedules the next
  // timer itself in the failure's own microtask domain (a state-driven
  // re-arm cannot be observed by the C9 poll-rearm acceptance check's
  // single-block fake-time advance, which drains only microtasks between fake
  // timers; this behaves identically in production where state flushes are
  // near-immediate). The
  // handle lives in pollTimerRef so the poll effect's cleanup cancels a
  // pending retry on any dep change (incl. scope), and arming is skipped
  // whenever a timer is already pending — at most one poll timer exists.
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const runPollFetchRef = useRef<(lease: Lease) => void>(() => undefined);

  const runPollFetch = useCallback(
    (lease: Lease): void => {
      // Advance the adaptive interval once per fired poll tick (x1.5, 30s
      // cap) — the same semantics the pre-fix timer callback had.
      pollIntervalMsRef.current = Math.min(pollIntervalMsRef.current * 1.5, 30_000);
      void fetchDocuments({ keepRowsOnFailure: true }).then((committed) => {
        // Success or dropped (stale scope/token): the effect re-arms from its
        // own deps after the commit, or the scope/query change already did.
        if (committed !== false) return;
        if (pollTimerRef.current !== null) return; // another arm owns the next tick
        if (!isContextLive(scope, lease)) return;
        if (readContextRef.current !== readContext) return;
        // Mirror the effect's arm condition: never poll without a processing
        // row (documentsRef mirrors documentsVisible at the last render).
        const stillProcessing = documentsRef.current.some(
          (document) =>
            document.metadata?.status === "processing" ||
            document.metadata?.status === "pending"
        );
        if (!stillProcessing) return;
        pollTimerRef.current = setTimeout(() => {
          pollTimerRef.current = null;
          if (!isContextLive(scope, lease)) return;
          if (readContextRef.current !== readContext) return;
          runPollFetchRef.current(lease);
        }, pollIntervalMsRef.current);
      });
      void fetchStats();
    },
    [fetchDocuments, fetchStats, isContextLive, readContext, scope]
  );
  runPollFetchRef.current = runPollFetch;

  // Per-document wiki fetch bookkeeping (#765 AC4/AC5):
  // - wikiDocAttemptRef counts, per document, every fetch START for that
  //   document (across batches). No whole-batch currency gate exists: batches
  //   always run to completion, and the per-document watermark orders the
  //   commits (review round 1 PRR-009).
  // - wikiDocCommittedRef is the per-document successful-commit watermark: a
  //   result commits when its attempt number exceeds it. A newer FAILURE
  //   never raises the watermark (a held older success still commits), a
  //   newer SUCCESS blocks a subsequent older late success (no stale
  //   clobber), and the failure path applies the same recency guard
  //   (review round 2 PRR-035).
  // - wikiRetryAttemptsRef/wikiRetrySkipTicksRef drive the capped
  //   per-document retry backoff (tick-based; see WIKI_RETRY_SKIP_TICKS);
  //   wikiFailedRef mirrors the render state for the workers.
  const wikiDocAttemptRef = useRef(new Map<string, number>());
  const wikiDocCommittedRef = useRef(new Map<string, number>());
  const wikiRetryAttemptsRef = useRef(new Map<string, number>());
  const wikiRetrySkipTicksRef = useRef(new Map<string, number>());
  const wikiFailedRef = useRef<Set<string>>(new Set());

  const markWikiFetchFailed = useCallback(
    (docId: string, scope: Scope, lease: Lease): void => {
      if (!isContextLive(scope, lease)) return;
      const attempts = (wikiRetryAttemptsRef.current.get(docId) ?? 0) + 1;
      wikiRetryAttemptsRef.current.set(docId, attempts);
      wikiRetrySkipTicksRef.current.set(
        docId,
        WIKI_RETRY_SKIP_TICKS[
          Math.min(attempts - 1, WIKI_RETRY_SKIP_TICKS.length - 1)
        ]
      );
      wikiFailedRef.current.add(docId);
      setWikiFailedIdsState((previous) => {
        if (!isContextLive(scope, lease)) return previous;
        const next = wikiFailedScopeRef.current === scope ? new Set(previous) : new Set<string>();
        wikiFailedScopeRef.current = scope;
        next.add(docId);
        return next;
      });
    },
    [isContextLive]
  );

  const clearWikiFetchFailure = useCallback(
    (docId: string, scope: Scope, lease: Lease): void => {
      if (!isContextLive(scope, lease)) return;
      if (!wikiFailedRef.current.has(docId)) return;
      wikiFailedRef.current.delete(docId);
      wikiRetryAttemptsRef.current.delete(docId);
      wikiRetrySkipTicksRef.current.delete(docId);
      setWikiFailedIdsState((previous) => {
        if (!isContextLive(scope, lease)) return previous;
        if (wikiFailedScopeRef.current !== scope) return previous;
        if (!previous.has(docId)) return previous;
        const next = new Set(previous);
        next.delete(docId);
        return next;
      });
    },
    [isContextLive]
  );

  const fetchWikiStatuses = useCallback(
    async (docs: Document[]) => {
      if (scopeRef.current !== scope || activeVaultId === null) return;
      const indexed = docs.filter((document) => document.metadata?.status === "indexed");
      if (indexed.length === 0) return;
      const key = JSON.stringify(indexed.map((document) => String(document.id)));
      const token = beginRead("wiki", key, scope);
      if (token === null) return;
      let cursor = 0;
      const worker = async () => {
        while (true) {
          // No whole-batch currency gate here (review round 1 PRR-009/R20):
          // a newer wiki read invalidating this batch's token used to stop
          // the workers mid-batch, leaving the remaining documents with no
          // cached status and no retry path. The per-document commit
          // watermark below keeps every result correct, so the batch always
          // runs to completion; scope death still stops it.
          if (!isContextLive(token.scope, token.lease)) return;
          const index = cursor;
          if (index >= indexed.length) return;
          cursor += 1;
          const document = indexed[index];
          const docId = String(document.id);
          const attempt = (wikiDocAttemptRef.current.get(docId) ?? 0) + 1;
          wikiDocAttemptRef.current.set(docId, attempt);
          try {
            const result = await getDocumentWikiStatus(
              Number(document.id),
              activeVaultId
            );
            // Per-document commit watermark (#765 AC5): scope liveness plus
            // attempt recency versus the last SUCCESSFUL commit for this
            // document — a superseded batch no longer discards an in-flight
            // result.
            if (!isContextLive(token.scope, token.lease)) return;
            if (attempt <= (wikiDocCommittedRef.current.get(docId) ?? 0)) return;
            wikiDocCommittedRef.current.set(docId, attempt);
            clearWikiFetchFailure(docId, token.scope, token.lease);
            setWikiStatusMapState((previous) => {
              if (!isContextLive(token.scope, token.lease)) return previous;
              const next = wikiScopeRef.current === token.scope ? { ...previous } : {};
              wikiScopeRef.current = token.scope;
              next[docId] = result;
              return next;
            });
          } catch {
            if (!isContextLive(token.scope, token.lease)) return;
            // Failure path carries the same attempt-recency guard as the
            // success path (review round 1 PRR-035): a stale failure must
            // not advance retry state a newer success already settled.
            if (attempt <= (wikiDocCommittedRef.current.get(docId) ?? 0)) return;
            markWikiFetchFailed(docId, token.scope, token.lease);
          }
        }
      };
      try {
        await Promise.all(
          Array.from({ length: Math.min(6, indexed.length) }, () => worker())
        );
      } finally {
        finishRead(token);
      }
    },
    [activeVaultId, beginRead, clearWikiFetchFailure, finishRead, isContextLive, markWikiFetchFailed, scope]
  );

  const compileInFlightRef = useRef(new Map<string, CompileToken>());
  const compileLatestRef = useRef(new Map<string, CompileToken>());
  const compileSerialRef = useRef(0);
  const compileTimersRef = useRef(new Set<CompileTimer>());
  // Upload-transition refetch timer + the seen-indexed reconciliation set
  // (#766 AC4/AC5). The timer is armed once per NEW upload→indexed transition
  // and cleared only on fire or lease retirement (see the scope effect).
  const uploadRefreshTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const knownIndexedUploadIdsRef = useRef<Set<string>>(new Set());
  // Every upload id ever seen (any status) — drives the acceptance refetch
  // trigger (review round 1 PRR-013). Persists across vault switches like
  // the indexed set: upload rows themselves survive switches, and a switch
  // must not make surviving rows look newly accepted.
  const knownUploadIdsRef = useRef<Set<string>>(new Set());
  const wikiPollFlightRef = useRef<WikiPollFlight | null>(null);
  const wikiPollSerialRef = useRef(0);

  const isMutationCurrent = useCallback(
    (token: CompileToken): boolean => isContextLive(token.scope, token.lease) &&
      compileLatestRef.current.get(token.docId) === token,
    [isContextLive]
  );

  const scheduleCompileWikiRefresh = useCallback(
    (token: CompileToken) => {
      if (!isMutationCurrent(token)) return;
      const entry: CompileTimer = {
        token,
        timer: undefined as unknown as ReturnType<typeof setTimeout>,
      };
      entry.timer = setTimeout(() => {
        compileTimersRef.current.delete(entry);
        if (!isMutationCurrent(token)) return;
        const currentDocs = documentsRef.current.filter(
          (document) => document.metadata?.status === "indexed"
        );
        if (currentDocs.length > 0) void fetchWikiStatuses(currentDocs);
      }, 2_000);
      compileTimersRef.current.add(entry);
    },
    [fetchWikiStatuses, isMutationCurrent]
  );

  const handleCompileDocument = useCallback(
    async (docId: string) => {
      if (activeVaultId === null) return;
      const lease = liveLeaseFor(scope);
      if (lease === null || !isContextLive(scope, lease)) return;
      const existing = compileInFlightRef.current.get(docId);
      if (existing !== undefined) {
        if (isMutationCurrent(existing)) return;
        if (compileInFlightRef.current.get(docId) === existing) {
          compileInFlightRef.current.delete(docId);
        }
      }
      const token: CompileToken = {
        scope,
        lease,
        serial: ++compileSerialRef.current,
        docId,
      };
      compileInFlightRef.current.set(docId, token);
      compileLatestRef.current.set(docId, token);
      setCompilingDocIdsState((previous) => {
        if (!isMutationCurrent(token)) return previous;
        const next = new Set(compilingScopeRef.current === token.scope ? previous : []);
        compilingScopeRef.current = token.scope;
        next.add(docId);
        return next;
      });
      try {
        const result = await compileDocumentWiki(Number(docId), activeVaultId);
        if (!isMutationCurrent(token)) return;
        // #765 F2: the route is idempotent — a returned `running` job means a
        // compile is already in flight, so the toast must not claim a fresh
        // queueing.
        toast.success(
          result.status === "running"
            ? "Wiki compile already in progress"
            : "Wiki compile job queued"
        );
        scheduleCompileWikiRefresh(token);
      } catch (error) {
        if (!isMutationCurrent(token)) return;
        toast.error(error instanceof Error ? error.message : "Failed to queue wiki compile");
      } finally {
        if (isMutationCurrent(token) && compileInFlightRef.current.get(docId) === token) {
          compileInFlightRef.current.delete(docId);
          setCompilingDocIdsState((previous) => {
            if (!isMutationCurrent(token)) return previous;
            const next = new Set(compilingScopeRef.current === token.scope ? previous : []);
            compilingScopeRef.current = token.scope;
            next.delete(docId);
            return next;
          });
        }
      }
    },
    [activeVaultId, isContextLive, isMutationCurrent, liveLeaseFor, scheduleCompileWikiRefresh, scope]
  );

  const guardedSetDocuments = useCallback(
    (action: SetStateAction<Document[]>) => {
      const lease = liveLeaseFor(scope);
      if (lease === null || !isContextLive(scope, lease)) return;
      setDocumentsState((previous) => {
        if (!isContextLive(scope, lease)) return previous;
        const ownedPrevious = documentsScopeRef.current === scope ? previous : [];
        documentsScopeRef.current = scope;
        return typeof action === "function" ? action(ownedPrevious) : action;
      });
    },
    [isContextLive, liveLeaseFor, scope]
  );

  const guardedSetStats = useCallback(
    (action: SetStateAction<DocumentStatsResponse | null>) => {
      const lease = liveLeaseFor(scope);
      if (lease === null || !isContextLive(scope, lease)) return;
      setStatsState((previous) => {
        if (!isContextLive(scope, lease)) return previous;
        const ownedPrevious = statsScopeRef.current === scope ? previous : null;
        statsScopeRef.current = scope;
        return typeof action === "function" ? action(ownedPrevious) : action;
      });
    },
    [isContextLive, liveLeaseFor, scope]
  );

  useEffect(() => {
    if (scopeRef.current !== scope || !isCurrentAuthOwner(scope.owner) ||
      captureAuthPrincipalGeneration() !== scope.principalGeneration) return;
    let lease = leaseRef.current;
    if (lease === null || lease.scope !== scope || lease.retired) {
      lease = { scope, serial: ++leaseSerialRef.current, retired: false };
      leaseRef.current = lease;
    }
    if (!isContextLive(scope, lease)) return;
    const mountedLease = lease;
    const compileTimers = compileTimersRef.current;
    // Copy the per-document collections out of the refs before the cleanup
    // closes over them (review round 2: eslint react-hooks/exhaustive-deps —
    // a ref value read at cleanup time is not guaranteed to be current; the
    // maps themselves are stable module-style holders, so clearing the
    // captured references clears the live collections).
    const docAttempts = wikiDocAttemptRef.current;
    const docCommitted = wikiDocCommittedRef.current;
    const retryAttempts = wikiRetryAttemptsRef.current;
    const retrySkipTicks = wikiRetrySkipTicksRef.current;
    const failedIds = wikiFailedRef.current;
    return () => {
      mountedLease.retired = true;
      for (const entry of compileTimers) {
        if (entry.token.lease === mountedLease) {
          clearTimeout(entry.timer);
          compileTimers.delete(entry);
        }
      }
      // The upload-transition refetch timer is owned by the lease lifecycle
      // (#766 AC4/AC5), NOT by the uploads effect — that effect's cleanup
      // runs on every uploads-array identity change and would starve the
      // timer under progress churn exactly as the pre-fix code did.
      if (uploadRefreshTimerRef.current !== null) {
        clearTimeout(uploadRefreshTimerRef.current);
        uploadRefreshTimerRef.current = null;
      }
      // Per-document wiki bookkeeping is scope-owned (review round 1
      // PRR-005/M2-03/B-3/R04): attempts, commit watermarks, retry
      // schedules, and the failed mirror are released with the scope so a
      // new vault/identity starts fresh and the collections cannot grow
      // without bound across a session.
      docAttempts.clear();
      docCommitted.clear();
      retryAttempts.clear();
      retrySkipTicks.clear();
      failedIds.clear();
      if (wikiPollFlightRef.current?.lease === mountedLease) {
        wikiPollFlightRef.current = null;
      }
    };
  }, [isContextLive, liveLeaseFor, scope]);

  const loadMore = useCallback(() => {
    const lease = liveLeaseFor(scope);
    if (lease === null || !isContextLive(scope, lease)) return;
    if (queryContextRef.current !== queryContext) return;
    setPageWindow((previous) => {
      if (!isContextLive(scope, lease) || queryContextRef.current !== queryContext) return previous;
      const size = previous.scope === scope && previous.query === queryBaseKey ? previous.size : PAGE_SIZE;
      return { scope, query: queryBaseKey, size: Math.min(size + PAGE_SIZE, MAX_PAGE_SIZE) };
    });
  }, [isContextLive, liveLeaseFor, queryBaseKey, queryContext, scope]);

  useEffect(() => {
    const lease = liveLeaseFor(scope);
    if (lease === null || !isContextLive(scope, lease)) return;
    let cancelled = false;
    const showSkeleton = initialSettledScopeRef.current !== scope;
    if (showSkeleton) commitLoading(scope, lease, true);
    void (async () => {
      await Promise.resolve();
      if (cancelled || !isContextLive(scope, lease) || readContextRef.current !== readContext) return;
      await Promise.all([fetchDocuments(), fetchStats()]);
      if (cancelled || !isContextLive(scope, lease)) return;
      initialSettledScopeRef.current = scope;
      if (showSkeleton) commitLoading(scope, lease, false);
    })();
    return () => {
      cancelled = true;
    };
  }, [commitLoading, fetchDocuments, fetchStats, isContextLive, liveLeaseFor, readContext, scope]);

  useEffect(() => {
    const lease = liveLeaseFor(scope);
    if (lease === null || !isContextLive(scope, lease)) return;
    const hasProcessingDocs = documentsVisible.some(
      (document) =>
        document.metadata?.status === "processing" ||
        document.metadata?.status === "pending"
    );
    if (!hasProcessingDocs) {
      pollIntervalMsRef.current = 2_000;
      return;
    }
    const delay = pollIntervalMsRef.current;
    const timer = setTimeout(() => {
      pollTimerRef.current = null;
      if (!isContextLive(scope, lease) || readContextRef.current !== readContext) return;
      runPollFetchRef.current(lease);
    }, delay);
    pollTimerRef.current = timer;
    return () => {
      clearTimeout(timer);
      if (pollTimerRef.current !== null) {
        clearTimeout(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    };
  }, [documentsVisible, isContextLive, liveLeaseFor, readContext, scope]);

  // Keyed on id AND status (#765 AC3): a processing→indexed transition with
  // an unchanged id set must re-enter the hydration effect — the id-only join
  // let the guard short-circuit it.
  const wikiDocKey = documentsVisible
    .map((document) => `${document.id}:${document.metadata?.status ?? ""}`)
    .join(",");
  const wikiHydrationRef = useRef<{ lease: Lease; key: string; context: object } | null>(null);
  useEffect(() => {
    const lease = liveLeaseFor(scope);
    if (lease === null || !isContextLive(scope, lease) || documentsVisible.length === 0) {
      return;
    }
    if (wikiHydrationRef.current?.lease === lease && wikiHydrationRef.current.key === wikiDocKey &&
      wikiHydrationRef.current.context === readContext) return;
    wikiHydrationRef.current = { lease, key: wikiDocKey, context: readContext };
    const need = documentsVisible.filter((document) => {
      if (document.metadata?.status !== "indexed") return false;
      // A failed document is owned exclusively by the retry interval
      // (review round 1 PRR-004): re-including it here re-requested it on
      // every unrelated key change, bypassing the capped schedule and —
      // once exhausted — re-requesting it forever.
      if (wikiFailedRef.current.has(String(document.id))) return false;
      const cached = wikiStatusVisible[String(document.id)];
      return !cached || isTransientWikiStatus(cached.wiki_status);
    });
    if (need.length > 0) void fetchWikiStatuses(need);
  }, [documentsVisible, fetchWikiStatuses, isContextLive, liveLeaseFor, readContext, scope, wikiDocKey, wikiStatusVisible]);

  const transientWikiKey = documentsVisible
    .filter(
      (document) =>
        document.metadata?.status === "indexed" &&
        isTransientWikiStatus(wikiStatusVisible[String(document.id)]?.wiki_status)
    )
    .map((document) => String(document.id))
    .join(",");

  // Scope-owned view of the failed set for render (#765 AC6): a vault/auth
  // switch can neither render another scope's failures nor retry them.
  const wikiFailedVisible = wikiFailedScopeRef.current === scope ? wikiFailedIds : new Set<string>();
  const wikiFailedRefLive = wikiFailedRef;
  // Retry scheduling joins the transient interval (#765 AC4): failed ids that
  // are visible, indexed, and inside their capped attempt schedule ride the
  // same 5 s tick; ids past the schedule are never re-requested.
  const wikiRetryKey = documentsVisible
    .filter(
      (document) =>
        document.metadata?.status === "indexed" &&
        wikiFailedRefLive.current.has(String(document.id)) &&
        (wikiRetryAttemptsRef.current.get(String(document.id)) ?? 0) <
          WIKI_RETRY_MAX_ATTEMPTS
    )
    .map((document) => String(document.id))
    .join(",");

  useEffect(() => {
    const lease = liveLeaseFor(scope);
    if (lease === null || !isContextLive(scope, lease)) return;
    if (!transientWikiKey && !wikiRetryKey) return;
    const timer = setInterval(() => {
      if (!isContextLive(scope, lease) || readContextRef.current !== readContext) return;
      const currentFlight = wikiPollFlightRef.current;
      if (currentFlight !== null) {
        if (isContextLive(currentFlight.scope, currentFlight.lease)) return;
        if (wikiPollFlightRef.current === currentFlight) wikiPollFlightRef.current = null;
      }
      const flight: WikiPollFlight = {
        scope,
        lease,
        serial: ++wikiPollSerialRef.current,
      };
      wikiPollFlightRef.current = flight;
      const wanted = new Set([
        ...transientWikiKey.split(",").filter(Boolean),
        ...wikiRetryKey.split(",").filter(Boolean),
      ]);
      const currentDocs = documentsRef.current.filter((document) => {
        const docId = String(document.id);
        if (!wanted.has(docId)) return false;
        if (transientWikiKey.split(",").includes(docId)) return true;
        // Retry-eligible only once its tick backoff has elapsed: each tick
        // first spends one skip credit before the id becomes eligible.
        const skip = wikiRetrySkipTicksRef.current.get(docId) ?? 0;
        if (skip > 0) {
          wikiRetrySkipTicksRef.current.set(docId, skip - 1);
          return false;
        }
        return true;
      });
      void fetchWikiStatuses(currentDocs).finally(() => {
        if (!isContextLive(flight.scope, flight.lease)) return;
        if (wikiPollFlightRef.current === flight) wikiPollFlightRef.current = null;
      });
    }, WIKI_POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [fetchWikiStatuses, isContextLive, liveLeaseFor, readContext, scope, transientWikiKey, wikiRetryKey]);

  useEffect(() => {
    const lease = liveLeaseFor(scope);
    if (lease === null || !isContextLive(scope, lease)) return;
    // Upload-transition refetch (#766 AC4/AC5, plus review round 1
    // PRR-013): the seen-set is RECONCILED to the currently-indexed ids on
    // every uploads change (an id that left `indexed` — e.g. retryUpload
    // returning it to pending — or disappeared is forgotten, so a genuine
    // later transition re-arms), and a NEW upload id (any status) arms the
    // refetch too: #766's constraint requires an accepted upload to appear
    // in the list within a bounded delay, not only once it reaches
    // `indexed`. Exactly ONE 1 s timer is armed per trigger round. The
    // timer is deliberately NOT cleared here: this effect's cleanup runs on
    // every uploads-array identity change (every progress tick), which is
    // what starved the old implementation; it is cleared on fire or lease
    // retirement instead.
    const currentlyIndexed = new Set(
      uploads
        .filter((upload) => upload.status === "indexed")
        .map((upload) => String(upload.id))
    );
    for (const id of knownIndexedUploadIdsRef.current) {
      if (!currentlyIndexed.has(id)) knownIndexedUploadIdsRef.current.delete(id);
    }
    const newlyIndexed = uploads.filter(
      (upload) =>
        upload.status === "indexed" &&
        !knownIndexedUploadIdsRef.current.has(String(upload.id))
    );
    newlyIndexed.forEach((upload) =>
      knownIndexedUploadIdsRef.current.add(String(upload.id))
    );
    let shouldRefetch = newlyIndexed.length > 0;
    if (!shouldRefetch) {
      // Acceptance trigger (PRR-013): an upload id we have never seen
      // appeared in the queue (pending/uploading) — refetch once so the new
      // document shows up promptly; monitor-tick re-creations of the same
      // ids arm nothing.
      for (const upload of uploads) {
        if (!knownUploadIdsRef.current.has(String(upload.id))) {
          shouldRefetch = true;
          break;
        }
      }
    }
    uploads.forEach((upload) => knownUploadIdsRef.current.add(String(upload.id)));
    if (!shouldRefetch) return;
    if (uploadRefreshTimerRef.current !== null) return;
    uploadRefreshTimerRef.current = setTimeout(() => {
      uploadRefreshTimerRef.current = null;
      if (!isContextLive(scope, lease) || readContextRef.current !== readContext) return;
      // keepRowsOnFailure (review round 1 PRR-011): a transient failure of
      // this background refetch must not wipe a loaded list to the error
      // state — the transition was consumed, so nothing would self-heal.
      void fetchDocuments({ keepRowsOnFailure: true });
      void fetchStats();
    }, 1_000);
    // uploads stays a dependency (transition detection), but its cleanup no
    // longer owns the timer — see the scope effect for teardown.
  }, [fetchDocuments, fetchStats, isContextLive, liveLeaseFor, readContext, scope, uploads]);

  return {
    documents: documentsVisible,
    setDocuments: guardedSetDocuments,
    stats: statsVisible,
    setStats: guardedSetStats,
    loading: loadingVisible,
    listError: listErrorVisible,
    fetchDocuments,
    fetchStats,
    wikiStatusMap: wikiStatusVisible,
    wikiFetchFailedIds: wikiFailedVisible,
    compilingDocIds: compilingVisible,
    handleCompileDocument,
    total: totalVisible,
    hasMore: documentsVisible.length < totalVisible,
    loadMore,
  };
}
