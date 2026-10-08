import { useReducer, useEffect, useMemo, useRef, useCallback, useSyncExternalStore } from "react";
import apiClient, { type HealthResponse } from "@/lib/api";
import { useAuthStore } from "@/stores/useAuthStore";
import type { HealthStatus } from "@/types/health";
import { useAuthOwner } from "./useAuthOwner";
import {
  captureAuthPrincipalGeneration,
  isCurrentAuthOwner,
  subscribeAuthPrincipal,
  type AuthOwner,
} from "@/lib/api/auth-lifecycle";

interface UseHealthCheckOptions {
  pollInterval?: number;
}

const DEEP_RECHECK_INTERVAL = 90_000;
const FAILURE_THRESHOLD = 2;

type HealthContext = {
  owner: AuthOwner;
  principalGeneration: number;
  isAuthenticated: boolean;
  pollInterval?: number;
};

type HealthLifetime = {
  context: HealthContext;
  active: boolean;
  isFirstCheck: boolean;
  lastDeepAt: number;
  hasRealServices: boolean;
  recheckAttempts: number;
  nextReadSequence: number;
  recheckTimer: { handle: ReturnType<typeof setTimeout>; token: symbol } | null;
  interval: ReturnType<typeof setInterval> | null;
};

type HealthRead = {
  context: HealthContext;
  lifetime: HealthLifetime;
  token: symbol;
  sequence: number;
};

const initialHealth: HealthStatus = {
  backend: false,
  embeddings: false,
  chat: false,
  loading: true,
  lastChecked: null,
};
type HealthOutcomeState = {
  health: HealthStatus;
  lifetime: HealthLifetime | null;
  lastPublishedSequence: number;
  failureStreak: number;
  hadSuccess: boolean;
};

type HealthOutcomeAction =
  | { type: "reset"; lifetime: HealthLifetime; isCurrent: () => boolean }
  | {
      type: "failure";
      lifetime: HealthLifetime;
      sequence: number;
      wasLatest: boolean;
      lastChecked: Date;
      isCurrent: () => boolean;
    }
  | {
      type: "success";
      lifetime: HealthLifetime;
      sequence: number;
      isCurrent: () => boolean;
      health: Omit<HealthStatus, "embeddings" | "chat"> & {
        embeddings?: boolean | null;
        chat?: boolean | null;
      };
    };

const initialHealthState: HealthOutcomeState = {
  health: initialHealth,
  lifetime: null,
  lastPublishedSequence: 0,
  failureStreak: 0,
  hadSuccess: false,
};

function healthOutcomeReducer(
  state: HealthOutcomeState,
  action: HealthOutcomeAction,
): HealthOutcomeState {
  if (action.type === "reset") {
    if (!action.isCurrent()) return state;
    return {
      health: initialHealth,
      lifetime: action.lifetime,
      lastPublishedSequence: 0,
      failureStreak: 0,
      hadSuccess: false,
    };
  }
  if (!action.isCurrent() || state.lifetime !== action.lifetime || action.sequence < state.lastPublishedSequence) {
    return state;
  }
  if (action.type === "success") {
    return {
      health: {
        ...action.health,
        embeddings: action.health.embeddings ?? state.health.embeddings,
        chat: action.health.chat ?? state.health.chat,
      },
      lifetime: state.lifetime,
      lastPublishedSequence: action.sequence,
      failureStreak: 0,
      hadSuccess: true,
    };
  }

  const failureStreak = state.failureStreak + 1;
  const shouldPublishDown =
    (action.wasLatest && !state.hadSuccess) || failureStreak >= FAILURE_THRESHOLD;
  if (shouldPublishDown) {
    return {
      ...state,
      health: { ...initialHealth, loading: false, lastChecked: action.lastChecked },
      lastPublishedSequence: action.sequence,
      failureStreak,
    };
  }
  if (action.wasLatest) {
    return {
      ...state,
      health: state.health.loading ? { ...state.health, loading: false } : state.health,
      lastPublishedSequence: action.sequence,
      failureStreak,
    };
  }
  return { ...state, failureStreak };
}

function useAuthPrincipalGeneration(): number {
  return useSyncExternalStore(
    subscribeAuthPrincipal,
    captureAuthPrincipalGeneration,
    captureAuthPrincipalGeneration,
  );
}

/** Polls backend health while keeping every result in its auth/principal lease. */
export function useHealthCheck(options?: UseHealthCheckOptions): HealthStatus {
  const authOwner = useAuthOwner();
  const principalGeneration = useAuthPrincipalGeneration();
  const isAuthenticated = useAuthStore((state) => state.isAuthenticated);
  const pollInterval = options?.pollInterval;
  const [healthState, dispatchHealth] = useReducer(healthOutcomeReducer, initialHealthState);
  const latestContextRef = useRef<HealthContext | null>(null);
  const healthContextRef = useRef<HealthContext | null>(null);
  const lifetimeRef = useRef<HealthLifetime | null>(null);
  // latestReadRef is deliberately persistent after a response settles. React
  // functional reducers may execute after the transport promise continuation.
  const latestReadRef = useRef<HealthRead | null>(null);
  const activeReadRef = useRef<HealthRead | null>(null);
  const checkHealthRef = useRef<() => Promise<void>>(() => Promise.resolve());

  const context = useMemo<HealthContext>(
    () => ({ owner: authOwner, principalGeneration, isAuthenticated, pollInterval }),
    [authOwner, isAuthenticated, pollInterval, principalGeneration],
  );
  latestContextRef.current = context;

  const isCurrentContext = useCallback((candidate: HealthContext): boolean => {
    return (
      latestContextRef.current === candidate &&
      isCurrentAuthOwner(candidate.owner) &&
      captureAuthPrincipalGeneration() === candidate.principalGeneration
    );
  }, []);

  const isCurrentLifetime = useCallback(
    (candidate: HealthContext, lifetime: HealthLifetime): boolean =>
      isCurrentContext(candidate) &&
      lifetime.active &&
      lifetime.context === candidate &&
      lifetimeRef.current === lifetime,
    [isCurrentContext],
  );

  const isCurrentRead = useCallback(
    (read: HealthRead): boolean =>
      isCurrentLifetime(read.context, read.lifetime) && latestReadRef.current === read,
    [isCurrentLifetime],
  );

  // A replacement render gets an owned loading view synchronously. The old
  // state is not returned while its replacement lifetime awaits its first read.
  const visibleHealth = healthState.lifetime?.context === context ? healthState.health : initialHealth;

  useEffect(() => {
    const lifetime: HealthLifetime = {
      context,
      active: true,
      isFirstCheck: true,


      lastDeepAt: 0,
      hasRealServices: false,
      recheckAttempts: 0,
      nextReadSequence: 1,
      recheckTimer: null,
      interval: null,
    };

    lifetimeRef.current = lifetime;
    healthContextRef.current = context;
    if (!isCurrentLifetime(context, lifetime)) return;
    if (isCurrentLifetime(context, lifetime)) dispatchHealth({ type: "reset", lifetime, isCurrent: () => isCurrentLifetime(context, lifetime) });

    const clearRecheck = (): void => {
      if (!isCurrentLifetime(context, lifetime)) return;
      if (lifetime.recheckTimer !== null) {
        clearTimeout(lifetime.recheckTimer.handle);
        lifetime.recheckTimer = null;
      }
    };

    const checkHealth = async (): Promise<void> => {
      if (!isCurrentLifetime(context, lifetime)) return;
      const read: HealthRead = { context, lifetime, token: Symbol("health-read"), sequence: lifetime.nextReadSequence++ };
      if (!isCurrentLifetime(context, lifetime)) return;
      latestReadRef.current = read;
      activeReadRef.current = read;

      const deep =
        context.isAuthenticated &&
        (lifetime.isFirstCheck ||
          Date.now() - lifetime.lastDeepAt >= DEEP_RECHECK_INTERVAL);
      const deepStartedAt = deep ? Date.now() : null;
      if (!isCurrentRead(read)) return;
      const deepWasFirstCheck = lifetime.isFirstCheck;
      if (deepStartedAt !== null) {
        if (!isCurrentRead(read)) return;
        lifetime.lastDeepAt = deepStartedAt;
        lifetime.isFirstCheck = false;
      }
      const params = deep ? { deep: true } : {};

      let response: { data: HealthResponse };
      let services: HealthResponse["services"] | undefined;
      try {
        if (!isCurrentRead(read)) return;
        response = await apiClient.get<HealthResponse>("/health", { params });
        const payload = response?.data;
        if (payload === null || typeof payload !== "object") {
          throw new Error("Malformed health response");
        }
        services = payload.services;
        if (services !== undefined && (services === null || typeof services !== "object")) {
          throw new Error("Malformed health services");
        }
      } catch {
        if (!isCurrentLifetime(context, lifetime)) return;
        if (deepStartedAt !== null && isCurrentRead(read) && deepWasFirstCheck) {
          lifetime.isFirstCheck = true;
        }
        dispatchHealth({
          type: "failure",
          lifetime,
          sequence: read.sequence,
          wasLatest: latestReadRef.current === read,
          lastChecked: new Date(),
          isCurrent: () => isCurrentLifetime(context, lifetime),
        });
        if (activeReadRef.current === read) activeReadRef.current = null;
        return;
      }

      if (!isCurrentRead(read)) return;
      lifetime.isFirstCheck = false;

      if (!isCurrentRead(read)) return;
      const servicesUnknown =
        !lifetime.hasRealServices &&
        services?.embeddings == null &&
        services?.chat == null;
      if (servicesUnknown && lifetime.recheckAttempts < 5) {
        if (!isCurrentRead(read)) return;
        lifetime.recheckAttempts += 1;
        clearRecheck();
        if (!isCurrentRead(read)) return;
        const timerToken = Symbol("health-recheck");
        const handle = setTimeout(() => {
          // The captured read must still be latest before consulting the ref;
          // otherwise A could accidentally invoke B's current closure.
          if (!isCurrentRead(read)) return;
          if (lifetime.recheckTimer?.token !== timerToken) return;
          lifetime.recheckTimer = null;
          void checkHealthRef.current();
        }, 2000);
        lifetime.recheckTimer = { handle, token: timerToken };
      } else if (!servicesUnknown) {
        if (!isCurrentRead(read)) return;
        lifetime.recheckAttempts = 0;
      }

      if (services?.embeddings != null && services?.chat != null) {
        if (!isCurrentRead(read)) return;
        lifetime.hasRealServices = true;
      }

      const newBackend = services?.backend ?? response.data.status === "ok";
      const stillChecking =
        servicesUnknown && !lifetime.hasRealServices && lifetime.recheckAttempts < 5;
      if (!isCurrentRead(read)) return;
      dispatchHealth({
        type: "success",
        lifetime,
        sequence: read.sequence,
        isCurrent: () => isCurrentRead(read),
        health: {
          backend: newBackend,
          embeddings: services?.embeddings,
          chat: services?.chat,
          loading: stillChecking,
          lastChecked: new Date(),
        },
      });
      if (activeReadRef.current === read) activeReadRef.current = null;
    };

    checkHealthRef.current = checkHealth;
    void checkHealth();
    if (pollInterval) {
      lifetime.interval = setInterval(() => {
        if (!isCurrentLifetime(context, lifetime)) return;
        void checkHealth();
      }, pollInterval);
    }

    return () => {
      lifetime.active = false;
      if (lifetime.recheckTimer !== null) {
        clearTimeout(lifetime.recheckTimer.handle);
        lifetime.recheckTimer = null;
      }
      if (lifetime.interval !== null) {
        clearInterval(lifetime.interval);
        lifetime.interval = null;
      }
      if (activeReadRef.current?.lifetime === lifetime) activeReadRef.current = null;
      if (lifetimeRef.current === lifetime) lifetimeRef.current = null;
      if (healthContextRef.current === context) healthContextRef.current = null;
    };
  }, [context, isCurrentLifetime, isCurrentRead, pollInterval]);

  return visibleHealth;
}
