/**
 * Quality-report API client (issue #237, PRODUCT-ENH-12).
 *
 * Structured user-facing quality feedback — incorrect_answer /
 * missing_source / stale_source / bad_extraction — bound to the message's
 * turn identity and provenance. Reports are converted by operators into
 * replayable evaluation cases and compared before/after a fix.
 */
import { apiClient } from "./core";

export const QUALITY_REPORT_CATEGORIES = [
  "incorrect_answer",
  "missing_source",
  "stale_source",
  "bad_extraction",
] as const;

export type QualityReportCategory = (typeof QUALITY_REPORT_CATEGORIES)[number];

export interface QualityReportProvenance {
  config_ref: string;
  source_file_hashes: string[];
}

export interface QualityReport {
  id: number;
  session_id: number;
  message_id: number;
  category: QualityReportCategory;
  note: string | null;
  turn_id: string | null;
  seq: number | null;
  provenance: QualityReportProvenance;
  created_at: string;
}

export async function submitQualityReport(
  sessionId: number,
  messageId: number,
  category: QualityReportCategory,
  note?: string
): Promise<QualityReport> {
  const response = await apiClient.post<QualityReport>("/quality/reports", {
    session_id: sessionId,
    message_id: messageId,
    category,
    note: note ?? null,
  });
  return response.data;
}

export async function listQualityReports(
  sessionId: number
): Promise<QualityReport[]> {
  const response = await apiClient.get<{ reports: QualityReport[] }>(
    "/quality/reports",
    { params: { session_id: sessionId } }
  );
  return response.data.reports;
}
