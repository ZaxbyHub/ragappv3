import { apiClient } from "./core";

// ---------------------------------------------------------------------------
// Unified cross-entity discovery search (Issue #515 / PRODUCT-ENH-11)
// Backed by GET /api/search/unified — documents, wiki pages, KMS entries and
// chat session titles in one SQL/FTS pass (no vector store involvement).
// ---------------------------------------------------------------------------

/** Entity kinds the unified endpoint accepts in its `types` parameter. */
export const UNIFIED_SEARCH_TYPES = ["document", "wiki", "kms", "chat"] as const;

export type UnifiedSearchType = (typeof UNIFIED_SEARCH_TYPES)[number];

export interface UnifiedSearchResult {
  /** Entity kind — discriminates the renderer and the url_hint target. */
  type: UnifiedSearchType;
  id: number;
  title: string;
  snippet: string;
  vault_id: number;
  /** Frontend path that opens the entity (e.g. "/documents/5", "/kms/7"). */
  url_hint: string;
  /** bm25 for documents; deterministic title > summary > body tier otherwise. */
  score: number;
}

export interface UnifiedSearchResponse {
  results: UnifiedSearchResult[];
}

export async function unifiedSearch(params: {
  q: string;
  vault_id?: number;
  /** Comma-separated subset of document,wiki,kms,chat. Omit for all. */
  types?: string;
  /** Maximum results per entity type (backend bounds 1..50). */
  limit?: number;
}): Promise<UnifiedSearchResponse> {
  const response = await apiClient.get<UnifiedSearchResponse>(
    "/search/unified",
    { params }
  );
  return response.data;
}
