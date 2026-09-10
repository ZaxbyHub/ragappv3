import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Loader2, Search } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { EmptyState } from "@/components/EmptyState";
import { PageTitleHeader } from "@/components/layout/PageTitleHeader";
import { VaultSelector } from "@/components/vault/VaultSelector";
import { useVaultStore } from "@/stores/useVaultStore";
import {
  UNIFIED_SEARCH_TYPES,
  unifiedSearch,
  type UnifiedSearchResult,
  type UnifiedSearchType,
} from "@/lib/api";

/**
 * Unified discovery surface (issue #515 / PRODUCT-ENH-11).
 *
 * The app shell's "Search across everything" control routes here with
 * ?q=…&types=…; this page owns the GET /search/unified call and renders the
 * cross-entity results (documents, wiki pages, KMS entries, chat sessions)
 * with type labels, vault provenance and an explicit empty state.
 */

const TYPE_LABELS: Record<UnifiedSearchType, string> = {
  document: "Document",
  wiki: "Wiki",
  kms: "KMS",
  chat: "Chat",
};

const TYPE_SHORT_LABELS: Record<UnifiedSearchType, string> = {
  document: "Docs",
  wiki: "Wiki",
  kms: "KMS",
  chat: "Chat",
};

/** Parse the `types` query param into a validated subset; absent/invalid
 * values fall back to "every entity type" (the backend's documented default). */
function typesFromParam(raw: string | null): UnifiedSearchType[] {
  if (!raw) return [...UNIFIED_SEARCH_TYPES];
  const parsed = raw
    .split(",")
    .map((part) => part.trim().toLowerCase())
    .filter((part): part is UnifiedSearchType =>
      (UNIFIED_SEARCH_TYPES as readonly string[]).includes(part)
    );
  return parsed.length > 0 ? Array.from(new Set(parsed)) : [...UNIFIED_SEARCH_TYPES];
}

export default function SearchPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const { activeVaultId, vaults } = useVaultStore();

  const q = searchParams.get("q")?.trim() ?? "";
  const typesParam = searchParams.get("types");

  const [inputValue, setInputValue] = useState(q);
  const [typeState, setTypeState] = useState<Record<UnifiedSearchType, boolean>>(() => {
    const initial = { document: false, wiki: false, kms: false, chat: false };
    for (const t of typesFromParam(typesParam)) initial[t] = true;
    return initial;
  });

  const [results, setResults] = useState<UnifiedSearchResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [searched, setSearched] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Keep the form in sync with the URL (shell searchbox navigation, back /
  // forward) without blocking typing between submits.
  useEffect(() => {
    setInputValue(q);
    const next: Record<UnifiedSearchType, boolean> = {
      document: false,
      wiki: false,
      kms: false,
      chat: false,
    };
    for (const t of typesFromParam(typesParam)) next[t] = true;
    setTypeState(next);
  }, [q, typesParam]);

  // Generation token: a slow response for a previous query must never
  // clobber the results of the newest one (same pattern as KMSPage).
  const requestGenRef = useRef(0);

  useEffect(() => {
    if (!q) {
      setResults([]);
      setSearched(false);
      setError(null);
      return;
    }
    const gen = ++requestGenRef.current;
    const selected = typesFromParam(typesParam);
    setLoading(true);
    setError(null);
    unifiedSearch({
      q,
      vault_id: activeVaultId ?? undefined,
      types: selected.length < UNIFIED_SEARCH_TYPES.length ? selected.join(",") : undefined,
      limit: 20,
    })
      .then((res) => {
        if (requestGenRef.current !== gen) return;
        setResults(res.results);
        setSearched(true);
      })
      .catch((err) => {
        if (requestGenRef.current !== gen) return;
        setError(err instanceof Error ? err.message : "Search failed");
      })
      .finally(() => {
        if (requestGenRef.current === gen) setLoading(false);
      });
  }, [q, typesParam, activeVaultId]);

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const query = inputValue.trim();
    if (!query) return;
    const next = new URLSearchParams({ q: query });
    const active = UNIFIED_SEARCH_TYPES.filter((t) => typeState[t]);
    if (active.length > 0 && active.length < UNIFIED_SEARCH_TYPES.length) {
      next.set("types", active.join(","));
    }
    setSearchParams(next);
  }

  const vaultName = (vaultId: number): string =>
    vaults.find((v) => v.id === vaultId)?.name ?? `Vault ${vaultId}`;

  return (
    <div className="space-y-6 animate-in fade-in duration-300 pb-12">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <PageTitleHeader
          title="Search"
          description="Search across documents, wiki, knowledge entries and chats"
        />
        <VaultSelector />
      </div>

      {/* Search form — same contract as the shell's global searchbox */}
      <form
        className="flex flex-col gap-3 sm:flex-row sm:items-center"
        onSubmit={handleSubmit}
        role="search"
        aria-label="Unified search"
      >
        <div className="relative flex-1 max-w-md">
          <Search
            className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground"
            aria-hidden="true"
          />
          <Input
            type="search"
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            placeholder="Search across everything…"
            aria-label="Search query"
            className="pl-10"
          />
        </div>
        <div className="flex items-center gap-3 flex-wrap">
          {UNIFIED_SEARCH_TYPES.map((t) => (
            <div key={t} className="flex items-center gap-1.5">
              <input
                type="checkbox"
                checked={typeState[t]}
                onChange={(e) => setTypeState((prev) => ({ ...prev, [t]: e.target.checked }))}
                aria-label={`Search ${TYPE_LABELS[t].toLowerCase()} results`}
                className="size-3.5 shrink-0 accent-primary"
              />
              <span className="text-xs text-muted-foreground select-none" aria-hidden="true">
                {TYPE_SHORT_LABELS[t]}
              </span>
            </div>
          ))}
        </div>
        <Button type="submit" disabled={loading || !inputValue.trim()}>
          {loading ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : <Search className="w-4 h-4 mr-2" />}
          Search
        </Button>
      </form>

      {/* Results */}
      {error ? (
        <p className="text-sm text-destructive" role="alert">{error}</p>
      ) : !q ? (
        <EmptyState
          icon={Search}
          title="Search across everything"
          description="Enter a query to find documents, wiki pages, knowledge entries and chats."
        />
      ) : loading && results.length === 0 ? (
        <div className="flex items-center justify-center py-12 text-sm text-muted-foreground">
          <Loader2 className="w-4 h-4 mr-2 animate-spin" />
          Searching…
        </div>
      ) : searched && results.length === 0 ? (
        <EmptyState
          icon={Search}
          title="No results found"
          description={`Nothing matched "${q}"${activeVaultId ? " in the selected vault" : ""}. Try a different query or widen the type filters.`}
        />
      ) : (
        <div className="space-y-3">
          <p className="text-xs text-muted-foreground">
            {results.length} {results.length === 1 ? "result" : "results"} for &ldquo;{q}&rdquo;
          </p>
          {results.map((result) => (
            <Card key={`${result.type}-${result.id}`}>
              <CardContent className="p-4">
                <div className="flex flex-wrap items-center gap-2 mb-1.5">
                  <Badge
                    variant={
                      result.type === "document"
                        ? "default"
                        : result.type === "wiki"
                          ? "secondary"
                          : "outline"
                    }
                  >
                    {TYPE_LABELS[result.type]}
                  </Badge>
                  <span className="text-xs text-muted-foreground">
                    {vaultName(result.vault_id)}
                  </span>
                </div>
                {/* url_hint is the backend-provided frontend path that opens
                    the entity — internal navigation, not an external href. */}
                <Link
                  to={result.url_hint}
                  className="text-sm font-medium hover:underline focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring rounded-sm"
                >
                  {result.title}
                </Link>
                {result.snippet && (
                  <p className="text-xs text-muted-foreground line-clamp-2 mt-1">
                    {result.snippet}
                  </p>
                )}
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
