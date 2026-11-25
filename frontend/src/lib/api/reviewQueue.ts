import { apiClient } from "./core";

// ---------------------------------------------------------------------------
// Unified reviewed-knowledge queue (issue #1163, Workstream L PR 7)
// ---------------------------------------------------------------------------

export type ReviewQueueKind = "wiki_page" | "kms_entry";

export interface ReviewQueueItem {
  kind: ReviewQueueKind;
  id: number;
  title: string;
  slug: string;
  status: string;
  file_id: number | null;
  updated_at: string | null;
  /** The source quote the item rests on (bounded excerpt; null when the
   *  item has no resolvable source). */
  source_quote: string | null;
}

export interface ReviewQueueResponse {
  items: ReviewQueueItem[];
  count: number;
}

export interface ReviewQueueTarget {
  kind: ReviewQueueKind;
  id: number;
}

export interface ReviewQueueBatchResponse {
  action: "approve" | "reject";
  updated: number;
  items: Array<{ kind: ReviewQueueKind; id: number; status: string }>;
}

export interface ListReviewQueueOptions {
  vault_id: number;
  kind?: ReviewQueueKind;
  status?: string;
}

export async function listReviewQueue(
  options: ListReviewQueueOptions
): Promise<ReviewQueueResponse> {
  const res = await apiClient.get<ReviewQueueResponse>("/review/queue", {
    params: {
      vault_id: options.vault_id,
      ...(options.kind ? { kind: options.kind } : {}),
      ...(options.status ? { status: options.status } : {}),
    },
  });
  return res.data;
}

export async function batchReviewQueue(
  vaultId: number,
  items: ReviewQueueTarget[],
  action: "approve" | "reject"
): Promise<ReviewQueueBatchResponse> {
  const res = await apiClient.post<ReviewQueueBatchResponse>(
    "/review/queue/batch",
    { vault_id: vaultId, items, action }
  );
  return res.data;
}
