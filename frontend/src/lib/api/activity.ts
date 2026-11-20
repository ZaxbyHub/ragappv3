/**
 * Vault-scoped activity API (issues #1157 / #1158, audit 2026-11-15).
 *
 * One read aggregates every background-job family for exactly the vaults
 * the caller can read — the endpoint the Activity tray polls once per tick
 * instead of the per-family, per-vault fan-out.
 */
import apiClient from "./core";

export type ActivityJobFamily =
  | "ingest"
  | "wiki"
  | "kms"
  | "draft-room"
  | "reindex";

/** One job row, whatever the family (family-specific fields are null
 * outside their family; `vault_id` is null only for admin-visible global
 * reindex rows; `progress_percent` is a float). */
export interface ActivityJob {
  family: ActivityJobFamily;
  id: number;
  vault_id: number | null;
  status: string;
  title: string;
  created_at: string | null;
  completed_at: string | null;
  draft_id: number | null;
  file_id: string | null;
  active_stage: string | null;
  phase: string | null;
  phase_message: string | null;
  progress_percent: number | null;
  processed_units: number | null;
  total_units: number | null;
  unit_label: string | null;
  error: string | null;
}

export interface ActivityJobsResponse {
  jobs: ActivityJob[];
  change_token: string;
  changed: boolean;
}

export interface GetActivityJobsOptions {
  /** Bounded per-family page (server default 50). */
  limit?: number;
  signal?: AbortSignal;
}

export async function getActivityJobs(
  options: GetActivityJobsOptions = {}
): Promise<ActivityJobsResponse> {
  const response = await apiClient.get<ActivityJobsResponse>("/activity/jobs", {
    params: options.limit != null ? { limit: options.limit } : undefined,
    signal: options.signal,
  });
  return response.data;
}
