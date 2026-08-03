/**
 * Whether a dirty-payload sent to PUT /settings should trigger invalidating
 * the Draft Room capabilities query. Extracted as a pure predicate (mirrors
 * handleInputChange.ts) so the SettingsPage save flow's invalidation
 * decision is directly testable without a full page render.
 *
 * Without this invalidation, the sidebar Draft Room link (gated by
 * useDraftRoomVisible(), which reads useDraftRoomCapabilities()) would only
 * pick up a newly-enabled flag after its 5-minute staleTime elapses.
 */
import type { UpdateSettingsRequest } from "@/lib/api/settings";

export function payloadTogglesDraftRoom(payload: UpdateSettingsRequest): boolean {
  return "draft_room_enabled" in payload;
}
