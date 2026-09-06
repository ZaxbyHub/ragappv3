import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { canvasKeys, getCanvasCapabilities, type CanvasCapabilities } from "@/lib/api/canvas";

const CAPABILITIES_STALE_TIME_MS = 5 * 60 * 1000;

export function useCanvasCapabilities(): UseQueryResult<CanvasCapabilities, Error> {
  return useQuery({
    queryKey: canvasKeys.capabilities(),
    queryFn: getCanvasCapabilities,
    staleTime: CAPABILITIES_STALE_TIME_MS,
    retry: false,
  });
}

/**
 * True only when the backend explicitly reports the canvas feature enabled.
 * The backend signals "disabled" with HTTP 503 (`canvas_disabled`) rather than
 * `{enabled: false}` — that surfaces here as an error, so returning `false`
 * for loading AND error states keeps every entry point fail-closed either way.
 */
export function useCanvasVisible(): boolean {
  const { data, isLoading, isError } = useCanvasCapabilities();
  if (isLoading || isError || !data) return false;
  return data.enabled === true;
}
