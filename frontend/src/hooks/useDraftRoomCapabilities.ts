import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { draftRoomKeys, getDraftRoomCapabilities, type DraftRoomCapabilities } from "@/lib/api/draftRoom";

const CAPABILITIES_STALE_TIME_MS = 5 * 60 * 1000;

export function useDraftRoomCapabilities(): UseQueryResult<DraftRoomCapabilities, Error> {
  return useQuery({
    queryKey: draftRoomKeys.capabilities(),
    queryFn: getDraftRoomCapabilities,
    staleTime: CAPABILITIES_STALE_TIME_MS,
    retry: false,
  });
}

/**
 * True only when the backend reports the feature enabled AND the full
 * editorial gates pipeline is installed AND ready-marking is available.
 * Returns `false` while loading or on error so nav/routes fail closed rather
 * than flashing a feature that isn't actually usable.
 */
export function useDraftRoomVisible(): boolean {
  const { data, isLoading, isError } = useDraftRoomCapabilities();
  if (isLoading || isError || !data) return false;
  return data.enabled === true && data.editorial_gates_installed === true && data.ready_available === true;
}
