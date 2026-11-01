import axios, { type AxiosResponse, type InternalAxiosRequestConfig } from "axios";
import { afterEach, expect, it, vi } from "vitest";
import { apiClient, getJwtAccessToken, resetCsrfToken, setJwtAccessToken } from "@/lib/api/core";
import { captureAuthOwner, isCurrentAuthOwner, reserveReplacementAuthOwner } from "@/lib/api/auth-lifecycle";

const originalAdapter = apiClient.defaults.adapter;

afterEach(() => {
  apiClient.defaults.adapter = originalAdapter;
  resetCsrfToken();
  setJwtAccessToken(null);
  vi.restoreAllMocks();
});

it("keeps the replacement session token when a physically dispatched retired data request returns token_invalid", async () => {
  reserveReplacementAuthOwner();
  setJwtAccessToken("session-A-token");
  let rejectA!: (error: unknown) => void;
  let dispatchedA!: () => void;
  const dispatched = new Promise<void>((resolve) => { dispatchedA = resolve; });
  let configA!: InternalAxiosRequestConfig;
  let dispatchCount = 0;
  apiClient.defaults.adapter = async (config) => {
    dispatchCount += 1;
    if (dispatchCount === 1) {
      configA = config;
      dispatchedA();
      return new Promise<AxiosResponse>((_, reject) => { rejectA = reject; });
    }
    return { config, status: 200, statusText: "OK", headers: {}, data: { current: true } };
  };
  // Observe rejection immediately; this is the real public Axios pipeline.
  const oldOutcome = apiClient.get("/issue775-owner-probe").then(
    (response) => ({ response, error: undefined }),
    (error: unknown) => ({ response: undefined, error }),
  );
  try {
    await dispatched;
    expect(configA.headers.Authorization).toBe("Bearer session-A-token");
    const ownerB = reserveReplacementAuthOwner();
    setJwtAccessToken("session-B-token");
    rejectA(new axios.AxiosError("Request failed with status code 401", "ERR_BAD_REQUEST", configA, undefined, {
      config: configA, status: 401, statusText: "Unauthorized", headers: {}, data: { detail: "token_invalid" },
    }));
    const outcome = await oldOutcome;
    expect(outcome.error).toBeDefined();
    expect(isCurrentAuthOwner(ownerB)).toBe(true);
    expect(captureAuthOwner()).toBe(ownerB);
    expect(getJwtAccessToken()).toBe("session-B-token");
    const current = await apiClient.get("/issue775-current-owner-probe");
    expect(current.data).toEqual({ current: true });
    expect(current.config.headers.Authorization).toBe("Bearer session-B-token");
    expect(dispatchCount).toBe(2);
  } finally {
    if (rejectA) rejectA(new Error("probe cleanup"));
    await oldOutcome;
  }
});
