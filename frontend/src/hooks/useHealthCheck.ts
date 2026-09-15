import { useState, useEffect, useCallback, useRef } from "react";
import apiClient, { type HealthResponse } from "@/lib/api";
import { useAuthStore } from "@/stores/useAuthStore";
import type { HealthStatus } from "@/types/health";

interface UseHealthCheckOptions {
  pollInterval?: number;
}

/** Deep re-check backstop: at least one real (deep) check this often, so a
 * retained "up" status can never go stale indefinitely. */
const DEEP_RECHECK_INTERVAL = 90_000;

/** Consecutive fetch failures required before services flip to "down", so a
 * single transient blip doesn't flash the reconnect banner. */
const FAILURE_THRESHOLD = 2;

/** Polls the backend health endpoint and returns service availability status.
 *
 * A `null`/absent service value from the backend means "not checked this
 * cycle" — the previous value is retained, never coerced to `false`. Only an
 * explicit `false` from the backend, or repeated fetch failures, mark a
 * service as down.
 */
export function useHealthCheck(options?: UseHealthCheckOptions): HealthStatus {
  const [health, setHealth] = useState<HealthStatus>({
    backend: false,
    embeddings: false,
    chat: false,
    loading: true,
    lastChecked: null,
  });

  const isFirstCheck = useRef(true);
  const failStreak = useRef(0);
  const hadSuccess = useRef(false);
  const lastDeepAt = useRef(0);
  const hasRealServices = useRef(false);
  const recheckTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const recheckAttempts = useRef(0);
  const checkHealthRef = useRef<() => Promise<void>>(() => Promise.resolve());
  const isAuthenticated = useAuthStore((state) => state.isAuthenticated);

  const clearRecheck = useCallback(() => {
    if (recheckTimer.current !== null) {
      clearTimeout(recheckTimer.current);
      recheckTimer.current = null;
    }
  }, []);

  useEffect(() => clearRecheck, [clearRecheck]);

  const checkHealth = useCallback(async (): Promise<void> => {
    // Deep probing now requires credentials on the backend (issue #551): an
    // unauthenticated deep=true would 401 and flap the reconnect banner for
    // anonymous visitors (the hook is mounted at the App root, login page
    // included). Shallow polls still serve the server-side last-known cache
    // and lazily refresh it, so anonymous users keep getting truthful
    // service status without triggering provider probes.
    const deep =
      isAuthenticated &&
      (isFirstCheck.current ||
        Date.now() - lastDeepAt.current >= DEEP_RECHECK_INTERVAL);
    try {
      // First check and periodic backstops include deep model probing;
      // other polls are lightweight (server serves cached last-known status)
      const params = deep ? { deep: true } : {};
      if (deep) lastDeepAt.current = Date.now();

      const response = await apiClient.get<HealthResponse>("/health", { params });
      // Success bookkeeping ONLY after the response resolves — resetting the
      // failure streak before the await would make the catch-side threshold
      // unreachable (every failure would observe a streak of 0/1).
      isFirstCheck.current = false;
      failStreak.current = 0;
      hadSuccess.current = true;
      const services = response.data.services;

      // A shallow poll against a cold server-side cache returns null
      // services ("not checked this cycle") with a background refresh now in
      // flight. While we hold no real booleans yet, stay in the `loading`
      // ("checking") state and re-poll shortly instead of publishing the
      // initial `false`s — otherwise the banner announces an outage from
      // unknowns (PR #606 review PRR-002). Bounded: once real booleans
      // arrive (authoritative true OR false) this never triggers again.
      const servicesUnknown =
        !hasRealServices.current &&
        services?.embeddings == null &&
        services?.chat == null;
      if (servicesUnknown && recheckAttempts.current < 5) {
        recheckAttempts.current += 1;
        clearRecheck();
        // Indirect through the ref so the re-check always runs the latest
        // closure (auth state may have changed since this one was created).
        recheckTimer.current = setTimeout(() => {
          void checkHealthRef.current();
        }, 2000);
      } else if (!servicesUnknown) {
        recheckAttempts.current = 0;
      }
      if (services?.embeddings != null && services?.chat != null) {
        hasRealServices.current = true;
      }

      const newBackend = services?.backend ?? response.data.status === "ok";
      // While unknown we hold the checking state; if the re-check budget is
      // exhausted without real booleans (server sweep genuinely not landing),
      // fall through to loading=false — the banner then shows its amber
      // "attempting to reconnect" state, which is accurate, and the regular
      // heartbeat keeps polling so a recovered server clears it.
      const stillChecking =
        servicesUnknown && !hasRealServices.current && recheckAttempts.current < 5;

      setHealth((prev) => ({
        backend: newBackend,
        // null/undefined = "not checked": retain last known value
        embeddings: services?.embeddings ?? prev.embeddings,
        chat: services?.chat ?? prev.chat,
        loading: stillChecking,
        lastChecked: new Date(),
      }));
    } catch {
      failStreak.current += 1;
      // Before the first successful check, surface failure immediately (the
      // initial state is already "down"); afterwards require consecutive
      // failures so transient blips don't flap the banner.
      if (hadSuccess.current && failStreak.current < FAILURE_THRESHOLD) {
        // Threshold grace: keep last-known services but still clear the
        // initial loading flag — a cold start with the backend down must
        // not spin forever (the banner renders once loading clears).
        setHealth((prev) => (prev.loading ? { ...prev, loading: false } : prev));
        return;
      }
      setHealth(() => ({
        backend: false,
        embeddings: false,
        chat: false,
        loading: false,
        lastChecked: new Date(),
      }));
    }
    // isAuthenticated is read inside the callback: without it the closure
    // would pin the mount-time auth state and never send deep after login.
  }, [isAuthenticated, clearRecheck]);

  useEffect(() => {
    checkHealthRef.current = checkHealth;
  }, [checkHealth]);

  useEffect(() => {
    checkHealth();

    if (options?.pollInterval) {
      const interval = setInterval(checkHealth, options.pollInterval);
      return () => clearInterval(interval);
    }
  }, [checkHealth, options?.pollInterval]);

  return health;
}
