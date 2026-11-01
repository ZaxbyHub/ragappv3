import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AxiosInstance, AxiosResponse, InternalAxiosRequestConfig } from "axios";

const { createdClients } = vi.hoisted(() => ({
  createdClients: [] as Array<{ defaults: { adapter?: unknown } }>,
}));

vi.mock("axios", async (importOriginal) => {
  const actual = await importOriginal<typeof import("axios")>();
  const realAxios = actual.default;

  return {
    ...actual,
    default: {
      ...realAxios,
      create: vi.fn((config: Parameters<typeof realAxios.create>[0]) => {
        const client = realAxios.create(config);
        createdClients.push(client);
        return client;
      }),
    },
  };
});

vi.mock("@/lib/api/onboarding", () => ({
  resetCitationReport: vi.fn(),
}));

vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: {
    getState: vi.fn(() => ({ fetchVaults: vi.fn().mockResolvedValue(undefined) })),
    setState: vi.fn(),
    subscribe: vi.fn(() => vi.fn()),
  },
}));

import { setJwtAccessToken } from "@/lib/api";
import {
  captureAuthOwner,
  reserveReplacementAuthOwner,
} from "@/lib/api/auth-lifecycle";
import { useAuthStore } from "./useAuthStore";

type User = {
  id: number;
  username: string;
  full_name: string;
  role: "superadmin" | "admin" | "member" | "viewer";
  is_active: boolean;
};

type PendingRequest = {
  config: InternalAxiosRequestConfig;
  settled: boolean;
  resolve: (response: AxiosResponse<unknown>) => void;
  reject: (reason: unknown) => void;
};

type Settled<T> =
  | { status: "fulfilled"; value: T }
  | { status: "rejected"; reason: unknown };

const adminUser: User = {
  id: 1,
  username: "admin-user",
  full_name: "Admin User",
  role: "admin",
  is_active: true,
};

const memberUser: User = {
  ...adminUser,
  full_name: "Member User",
  role: "member",
};

const positiveUser: User = {
  ...adminUser,
  full_name: "Current User",
};

const pendingRequests: PendingRequest[] = [];
let releasingPendingRequests = false;

function observe<T>(promise: Promise<T>): Promise<Settled<T>> {
  return promise.then(
    (value) => ({ status: "fulfilled", value }),
    (reason: unknown) => ({ status: "rejected", reason }),
  );
}

function responseFor(request: PendingRequest, data: unknown): AxiosResponse<unknown> {
  return {
    data,
    status: 200,
    statusText: "OK",
    headers: {},
    config: request.config,
  };
}

function resolveRequest(request: PendingRequest, data: unknown): void {
  if (request.settled) return;
  request.settled = true;
  request.resolve(responseFor(request, data));
}

function rejectRequest(request: PendingRequest, reason: unknown): void {
  if (request.settled) return;
  request.settled = true;
  request.reject(reason);
}

function releasePendingRequests(): void {
  releasingPendingRequests = true;
  for (const request of pendingRequests) {
    resolveRequest(request, {});
  }
}

async function requestAt(index: number, method: string): Promise<PendingRequest> {
  await vi.waitFor(() => {
    expect(pendingRequests[index]?.config.method?.toUpperCase()).toBe(method);
  });
  return pendingRequests[index];
}

function installRealAxiosAdapter(): void {
  const adapter = (config: InternalAxiosRequestConfig) =>
    new Promise<AxiosResponse<unknown>>((resolve, reject) => {
      const request = { config, settled: false, resolve, reject };
      pendingRequests.push(request);
      if (releasingPendingRequests) resolveRequest(request, {});
    });

  for (const client of createdClients) {
    client.defaults.adapter = adapter as AxiosInstance["defaults"]["adapter"];
  }
}

function resetStore(): void {
  reserveReplacementAuthOwner();
  setJwtAccessToken("session-token");
  useAuthStore.setState({
    user: null,
    accessToken: "session-token",
    isAuthenticated: true,
    isInitialized: true,
    isLoading: false,
    needsSetup: false,
    authMode: "jwt",
  });
  useAuthStore.setState({ user: adminUser });
}

describe("useAuthStore profile ownership and request races for issue #775", () => {
  beforeEach(() => {
    releasingPendingRequests = false;
    pendingRequests.length = 0;
    installRealAxiosAdapter();
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async () =>
        new Response(JSON.stringify({ csrf_token: "csrf-token" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    resetStore();
  });

  afterEach(() => {
    releasePendingRequests();
    setJwtAccessToken(null);
    reserveReplacementAuthOwner();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("does not let a held GET cross a same-owner principal downgrade/ABA, then accepts B", async () => {
    const owner = captureAuthOwner();
    const observed: Promise<Settled<void>>[] = [];
    const fetchA = useAuthStore.getState().fetchMe();
    observed.push(observe(fetchA));

    try {
      const requestA = await requestAt(0, "GET");
      expect(requestA.config.signal).toBeDefined();
      expect(requestA.config.signal).not.toBe(owner.signal);

      useAuthStore.setState({ user: memberUser });
      useAuthStore.setState({ user: adminUser });

      expect(captureAuthOwner()).toBe(owner);
      expect(owner.signal.aborted).toBe(false);
      expect(requestA.config.signal?.aborted).toBe(true);

      const fetchB = useAuthStore.getState().fetchMe();
      observed.push(observe(fetchB));
      const requestB = await requestAt(1, "GET");
      expect(requestB.config.signal).not.toBe(owner.signal);
      resolveRequest(requestA, { ...adminUser, role: "superadmin", full_name: "Stale A" });
      resolveRequest(requestB, positiveUser);
      await Promise.all(observed);

      expect(useAuthStore.getState().user).toEqual(positiveUser);
      expect(useAuthStore.getState().isAuthenticated).toBe(true);
    } finally {
      releasePendingRequests();
      await Promise.all(observed);
    }
  });

  it("does not let held A PATCH restore privilege after a same-owner principal change, then commits B", async () => {
    const owner = captureAuthOwner();
    const observed: Promise<Settled<void>>[] = [];
    const updateA = useAuthStore.getState().updateProfile({ full_name: "Stale A" });
    observed.push(observe(updateA));

    try {
      const requestA = await requestAt(0, "PATCH");
      useAuthStore.setState({ user: memberUser });
      expect(owner.signal.aborted).toBe(false);
      expect(requestA.config.signal?.aborted).toBe(true);

      const updateB = useAuthStore.getState().updateProfile({ full_name: "Current B" });
      observed.push(observe(updateB));
      const requestB = await requestAt(1, "PATCH");
      expect(requestB.config.signal).not.toBe(owner.signal);
      resolveRequest(requestA, { ...adminUser, full_name: "Stale A" });
      resolveRequest(requestB, { ...memberUser, full_name: "Current B" });
      await Promise.all(observed);

      expect(useAuthStore.getState().user).toEqual({ ...memberUser, full_name: "Current B" });
      expect(useAuthStore.getState().user?.role).toBe("member");
      expect(useAuthStore.getState().isLoading).toBe(false);
    } finally {
      releasePendingRequests();
      await Promise.all(observed);
    }
  });

  it("keeps loading true when A finishes before the concurrent latest B PATCH", async () => {
    const observed: Promise<Settled<void>>[] = [];
    const updateA = useAuthStore.getState().updateProfile({ full_name: "A" });
    observed.push(observe(updateA));

    try {
      const requestA = await requestAt(0, "PATCH");
      const updateB = useAuthStore.getState().updateProfile({ full_name: "B" });
      observed.push(observe(updateB));
      const requestB = await requestAt(1, "PATCH");
      expect(requestA.config.signal).not.toBe(requestB.config.signal);

      resolveRequest(requestA, { ...adminUser, full_name: "A" });
      await observed[0];
      expect(useAuthStore.getState().isLoading).toBe(true);

      resolveRequest(requestB, { ...adminUser, full_name: "B" });
      await Promise.all(observed);
      expect(useAuthStore.getState().user).toEqual({ ...adminUser, full_name: "B" });
      expect(useAuthStore.getState().isLoading).toBe(false);
    } finally {
      releasePendingRequests();
      await Promise.all(observed);
    }
  });

  it("invalidates a GET started before PATCH so the old read cannot undo the committed name", async () => {
    const observed: Promise<Settled<void>>[] = [];
    const fetchA = useAuthStore.getState().fetchMe();
    observed.push(observe(fetchA));

    try {
      const requestA = await requestAt(0, "GET");
      const update = useAuthStore.getState().updateProfile({ full_name: "Committed Name" });
      observed.push(observe(update));
      const requestPatch = await requestAt(1, "PATCH");

      resolveRequest(requestPatch, { ...adminUser, full_name: "Committed Name" });
      await observed[1];
      expect(useAuthStore.getState().user?.full_name).toBe("Committed Name");

      resolveRequest(requestA, { ...adminUser, full_name: "Stale Read" });
      await Promise.all(observed);
      expect(useAuthStore.getState().user?.full_name).toBe("Committed Name");
      expect(useAuthStore.getState().isLoading).toBe(false);
    } finally {
      releasePendingRequests();
      await Promise.all(observed);
    }
  });

  it("keeps a retained old-owner response from changing new-owner data or loading", async () => {
    const ownerA = captureAuthOwner();
    const observed: Promise<Settled<void>>[] = [];
    const updateA = useAuthStore.getState().updateProfile({ full_name: "Old Owner" });
    observed.push(observe(updateA));

    try {
      const requestA = await requestAt(0, "PATCH");
      const ownerB = reserveReplacementAuthOwner();
      useAuthStore.setState({ user: { ...positiveUser, full_name: "New Owner" }, isLoading: false });

      expect(ownerA.signal.aborted).toBe(true);
      expect(ownerB.signal.aborted).toBe(false);
      expect(requestA.config.signal?.aborted).toBe(true);

      resolveRequest(requestA, { ...adminUser, full_name: "Old Owner" });
      await Promise.all(observed);
      expect(useAuthStore.getState().user).toEqual({ ...positiveUser, full_name: "New Owner" });
      expect(useAuthStore.getState().isLoading).toBe(false);
    } finally {
      releasePendingRequests();
      await Promise.all(observed);
    }
  });

  it("accepts current GET/PATCH success and preserves the user while clearing ordinary PATCH failure", async () => {
    const observed: Promise<Settled<void>>[] = [];

    try {
      const fetchCurrent = useAuthStore.getState().fetchMe();
      observed.push(observe(fetchCurrent));
      const requestGet = await requestAt(0, "GET");
      resolveRequest(requestGet, positiveUser);
      expect((await observed[0]).status).toBe("fulfilled");
      expect(useAuthStore.getState().user).toEqual(positiveUser);

      const updateCurrent = useAuthStore.getState().updateProfile({ full_name: "Updated" });
      observed.push(observe(updateCurrent));
      const requestPatch = await requestAt(1, "PATCH");
      resolveRequest(requestPatch, { ...positiveUser, full_name: "Updated" });
      expect((await observed[1]).status).toBe("fulfilled");
      expect(useAuthStore.getState().user?.full_name).toBe("Updated");
      expect(useAuthStore.getState().isLoading).toBe(false);

      const failedUpdate = useAuthStore.getState().updateProfile({ full_name: "Rejected" });
      observed.push(observe(failedUpdate));
      const requestFailure = await requestAt(2, "PATCH");
      rejectRequest(requestFailure, new Error("profile network outage"));
      const failure = await observed[2];
      expect(failure.status).toBe("rejected");
      expect(useAuthStore.getState().user?.full_name).toBe("Updated");
      expect(useAuthStore.getState().isLoading).toBe(false);
    } finally {
      releasePendingRequests();
      await Promise.all(observed);
    }
  });
});
