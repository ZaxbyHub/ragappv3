import { useState, useCallback, useEffect, useRef } from "react";
import { toast } from "sonner";
import { useDebounce } from "./useDebounce";
import { searchMemories, listMemories, type MemoryResult } from "@/lib/api";
import { useTestMode } from "@/fixtures/TestModeContext";
import { mockMemories } from "@/fixtures/memories";

export interface UseMemorySearchReturn {
  memories: MemoryResult[];
  searchQuery: string;
  setSearchQuery: (query: string) => void;
  loading: boolean;
  handleSearch: () => Promise<void>;
}

/** Manages memory search with debounced queries and abort-on-unmount cleanup. */
export function useMemorySearch(activeVaultId: number | null): UseMemorySearchReturn {
  const testMode = useTestMode();
  const [memories, setMemories] = useState<MemoryResult[]>(testMode ? mockMemories : []);
  const [searchQuery, setSearchQuery] = useState("");
  const [debouncedSearchQuery] = useDebounce(searchQuery, 300);
  const [loading, setLoading] = useState(false);
  const abortControllerRef = useRef<AbortController | null>(null);

  const handleSearch = useCallback(async () => {
    // Cancel any pending request
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }
    const abortController = new AbortController();
    abortControllerRef.current = abortController;

    setLoading(true);
    try {
      if (testMode) {
        if (!abortController.signal.aborted) {
          if (debouncedSearchQuery.trim()) {
            const q = debouncedSearchQuery.toLowerCase();
            setMemories(mockMemories.filter((m) => m.content.toLowerCase().includes(q)));
          } else {
            setMemories(mockMemories);
          }
        }
        return;
      }
      if (debouncedSearchQuery.trim()) {
        // Search mode — use POST /memories/search
        const response = await searchMemories(
          { query: debouncedSearchQuery, limit: 50 },
          abortController.signal,
          activeVaultId ?? undefined
        );
        if (!abortController.signal.aborted) {
          setMemories(response.results || []);
        }
      } else {
        // List mode — use GET /memories
        const response = await listMemories(activeVaultId ?? undefined);
        if (!abortController.signal.aborted) {
          setMemories(response.memories || []);
        }
      }
    } catch (err) {
      if (err instanceof Error && err.name === "AbortError") {
        return;
      }
      console.error("Failed to load memories:", err);
      toast.error(err instanceof Error ? err.message : "Failed to load memories");
    } finally {
      if (!abortController.signal.aborted) {
        setLoading(false);
      }
    }
  }, [debouncedSearchQuery, activeVaultId, testMode]);

  useEffect(() => {
    handleSearch();
    return () => {
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }
    };
  }, [handleSearch]);

  return {
    memories,
    searchQuery,
    setSearchQuery,
    loading,
    handleSearch,
  };
}

// Re-export MemoryResult type for convenience
export type { MemoryResult };
