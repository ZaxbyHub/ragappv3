import { useState, useCallback, useEffect, useMemo, useRef, useSyncExternalStore } from "react";
import { toast } from "sonner";
import { useDebounce } from "./useDebounce";
import { searchMemories, listMemories, type MemoryResult } from "@/lib/api";
import { useTestMode } from "@/fixtures/TestModeContext";
import { mockMemories } from "@/fixtures/memories";
import { useAuthOwner } from "./useAuthOwner";
import {
  captureAuthPrincipalGeneration,
  isCurrentAuthOwner,
  subscribeAuthPrincipal,
  type AuthOwner,
} from "@/lib/api/auth-lifecycle";

export interface UseMemorySearchReturn {
  memories: MemoryResult[];
  searchQuery: string;
  setSearchQuery: (query: string) => void;
  loading: boolean;
  handleSearch: () => Promise<void>;
  retry: () => Promise<void>;
}

type SearchScope = {
  owner: AuthOwner;
  principalGeneration: number;
  activeVaultId: number | null;
  testMode: boolean;
};

type SearchContext = SearchScope & {
  scope: SearchScope;
  searchQuery: string;
  debouncedSearchQuery: string;
};

type SearchLease = {
  scope: SearchScope;
  token: symbol;
  active: boolean;
};

type SearchAttempt = {
  context: SearchContext;
  lease: SearchLease;
  token: symbol;
  controller: AbortController;
};

type MemoryEnvelope = {
  scope: SearchScope;
  context: SearchContext;
  memories: MemoryResult[];
};

type LoadingEnvelope = {
  scope: SearchScope;
  context: SearchContext;
  loading: boolean;
};

function useAuthPrincipalGeneration(): number {
  return useSyncExternalStore(
    subscribeAuthPrincipal,
    captureAuthPrincipalGeneration,
    captureAuthPrincipalGeneration,
  );
}

/** Manages memory search with debounced queries and auth/vault-owned reads. */
export function useMemorySearch(activeVaultId: number | null): UseMemorySearchReturn {
  const testMode = useTestMode();
  const authOwner = useAuthOwner();
  const principalGeneration = useAuthPrincipalGeneration();
  const [searchQuery, setSearchQuery] = useState("");
  const [debouncedSearchQuery] = useDebounce(searchQuery, 300);

  // The scope controls which rows/loading state may render. Raw query text is
  // intentionally excluded so same-owner/vault refreshes retain current rows.
  const scope = useMemo<SearchScope>(
    () => ({ owner: authOwner, principalGeneration, activeVaultId, testMode }),
    [activeVaultId, authOwner, principalGeneration, testMode],
  );
  const context = useMemo<SearchContext>(
    () => ({ ...scope, scope, searchQuery, debouncedSearchQuery }),
    [debouncedSearchQuery, scope, searchQuery],
  );
  const [memoryEnvelope, setMemoryEnvelope] = useState<MemoryEnvelope>(() => ({
    scope,
    context,
    memories: testMode ? mockMemories : [],
  }));
  const [loadingEnvelope, setLoadingEnvelope] = useState<LoadingEnvelope>(() => ({
    scope,
    context,
    loading: false,
  }));
  const latestContextRef = useRef<SearchContext | null>(null);
  const activeLeaseRef = useRef<SearchLease | null>(null);
  // Publication identity persists after transport cleanup so deferred React
  // functional updaters can still recognize their exact completed attempt.
  const latestPublicationAttemptRef = useRef<SearchAttempt | null>(null);
  const activeTransportRef = useRef<SearchAttempt | null>(null);
  const previousIntentRef = useRef({
    scope,
    raw: searchQuery,
    debounced: debouncedSearchQuery,
  });
  latestContextRef.current = context;

  const isCurrentContext = useCallback((candidate: SearchContext): boolean => {
    return (
      latestContextRef.current === candidate &&
      isCurrentAuthOwner(candidate.owner) &&
      captureAuthPrincipalGeneration() === candidate.principalGeneration
    );
  }, []);

  const isCurrentLease = useCallback(
    (candidate: SearchContext, lease: SearchLease): boolean =>
      isCurrentContext(candidate) &&
      lease.active &&
      lease.scope === candidate.scope &&
      activeLeaseRef.current === lease,
    [isCurrentContext],
  );

  const runSearch = useCallback(
    async (candidate: SearchContext, lease: SearchLease): Promise<void> => {
      if (!isCurrentLease(candidate, lease)) return;

      const previous = activeTransportRef.current;
      if (previous?.lease === lease) {
        if (!isCurrentLease(candidate, lease)) return;
        previous.controller.abort();
      }

      if (!isCurrentLease(candidate, lease)) return;
      const attempt: SearchAttempt = {
        context: candidate,
        lease,
        token: Symbol("memory-search-attempt"),
        controller: new AbortController(),
      };
      if (!isCurrentLease(candidate, lease)) return;
      latestPublicationAttemptRef.current = attempt;
      activeTransportRef.current = attempt;
      const isCurrentAttempt = (): boolean =>
        isCurrentLease(candidate, lease) &&
        latestPublicationAttemptRef.current === attempt &&
        !attempt.controller.signal.aborted;
      setLoadingEnvelope((previousLoading) =>
        isCurrentAttempt()
          ? { scope, context: candidate, loading: true }
          : previousLoading,
      );

      try {
        if (candidate.testMode) {
          if (!isCurrentAttempt()) return;
          const query = candidate.debouncedSearchQuery.trim().toLowerCase();
          const nextMemories = query
            ? mockMemories.filter((memory) => memory.content.toLowerCase().includes(query))
            : mockMemories;
          if (!isCurrentAttempt()) return;
          setMemoryEnvelope((previousEnvelope) =>
            isCurrentAttempt()
              ? { scope, context: candidate, memories: nextMemories }
              : previousEnvelope,
          );
          return;
        }

        let nextMemories: MemoryResult[];
        if (candidate.debouncedSearchQuery.trim()) {
          const response = await searchMemories(
            { query: candidate.debouncedSearchQuery, limit: 50 },
            attempt.controller.signal,
            candidate.activeVaultId ?? undefined,
          );
          if (!isCurrentAttempt()) return;
          if (!Array.isArray(response.results)) {
            throw new Error("Failed to load memories: malformed search response");
          }
          nextMemories = response.results;
        } else {
          const response = await listMemories(candidate.activeVaultId ?? undefined);
          if (!isCurrentAttempt()) return;
          if (!Array.isArray(response.memories)) {
            throw new Error("Failed to load memories: malformed list response");
          }
          nextMemories = response.memories;
        }

        // Keep the response inside an owned envelope. The invocation and the
        // functional reducer both re-check the exact context and attempt.
        const envelope = { scope, context: candidate, attempt, memories: nextMemories };
        if (!isCurrentAttempt() || envelope.context !== latestContextRef.current) return;
        setMemoryEnvelope((previousEnvelope) =>
          isCurrentAttempt() &&
          envelope.context === latestContextRef.current
            ? { scope: envelope.scope, context: envelope.context, memories: envelope.memories }
            : previousEnvelope,
        );
      } catch (error) {
        if (!isCurrentAttempt()) return;
        if (error instanceof Error && error.name === "AbortError") return;
        console.error("Failed to load memories:", error);
        if (!isCurrentAttempt()) return;
        toast.error(error instanceof Error ? error.message : "Failed to load memories");
      } finally {
        if (isCurrentAttempt()) {
          setLoadingEnvelope((previousLoading) =>
            isCurrentAttempt()
              ? { scope, context: candidate, loading: false }
              : previousLoading,
          );
          // Clear only the transport slot. latestPublicationAttemptRef remains
          // until a newer attempt, context, or lease supersedes its reducers.
          if (isCurrentAttempt() && activeTransportRef.current === attempt) {
            activeTransportRef.current = null;
          }
        }
      }
    },
    [isCurrentLease, scope],
  );

  const handleSearch = useCallback((): Promise<void> => {
    if (!isCurrentContext(context)) return Promise.resolve();
    const lease = activeLeaseRef.current;
    if (!lease || lease.scope !== scope || !lease.active) return Promise.resolve();
    return runSearch(context, lease);
  }, [context, isCurrentContext, runSearch, scope]);

  const guardedSetSearchQuery = useCallback(
    (query: string): void => {
      if (!isCurrentContext(context)) return;
      setSearchQuery((previousQuery) =>
        isCurrentContext(context) ? query : previousQuery,
      );
    },
    [context, isCurrentContext],
  );

  useEffect(() => {
    // Raw query replacement retires an old pending read without starting a
    // new network request. The mounted auth/vault lease remains usable for a
    // current manual handler and for the eventual debounced effect.
    if (!isCurrentContext(context)) return;
    const previousIntent = previousIntentRef.current;
    previousIntentRef.current = {
      scope,
      raw: searchQuery,
      debounced: debouncedSearchQuery,
    };
    const active = activeTransportRef.current;
    const latest = latestPublicationAttemptRef.current;
    if (active?.context === context && latest?.context === context) return;
    if (active?.lease.scope === scope) {
      active.controller.abort();
      if (!isCurrentContext(context)) return;
      if (activeTransportRef.current === active) activeTransportRef.current = null;
    }
    if (!isCurrentContext(context)) return;
    if (
      latest?.lease.scope === scope &&
      latest.context !== context &&
      latestPublicationAttemptRef.current === latest
    ) {
      latestPublicationAttemptRef.current = null;
    }
    setLoadingEnvelope((previousLoading) =>
      isCurrentContext(context) && previousLoading.scope === scope
        ? { scope, context, loading: false }
        : previousLoading,
    );
    const lease = activeLeaseRef.current;
    if (
      previousIntent.scope === scope &&
      previousIntent.raw !== searchQuery &&
      previousIntent.debounced === debouncedSearchQuery &&
      searchQuery === debouncedSearchQuery &&
      lease?.active &&
      lease.scope === scope &&
      activeLeaseRef.current === lease &&
      isCurrentContext(context)
    ) {
      void runSearch(context, lease);
    }
  }, [
    context,
    debouncedSearchQuery,
    isCurrentContext,
    runSearch,
    scope,
    searchQuery,
  ]);

  useEffect(() => {
    const lease: SearchLease = {
      scope,
      token: Symbol("memory-search-lease"),
      active: true,
    };
    activeLeaseRef.current = lease;
    // Owner/principal publication and the parent vault render can occur in
    // the same synchronous transition. Admit only the surviving exact lease
    // after that transition, without dispatching an intermediate vault read.
    void Promise.resolve().then(() => runSearch(context, lease));

    return () => {
      lease.active = false;
      const attempt = activeTransportRef.current;
      if (attempt?.lease === lease) {
        attempt.controller.abort();
        if (activeTransportRef.current === attempt) activeTransportRef.current = null;
      }
      if (activeLeaseRef.current === lease) activeLeaseRef.current = null;
    };
    // Raw query changes retire publication through latestContextRef but keep
    // network starts debounced until debouncedSearchQuery changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeVaultId, authOwner, debouncedSearchQuery, principalGeneration, scope, testMode, runSearch]);

  return {
    memories: memoryEnvelope.scope === scope ? memoryEnvelope.memories : [],
    searchQuery,
    setSearchQuery: guardedSetSearchQuery,
    loading: loadingEnvelope.scope === scope ? loadingEnvelope.loading : true,
    handleSearch,
    retry: handleSearch,
  };
}

export type { MemoryResult };
