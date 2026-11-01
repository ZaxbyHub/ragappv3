// swarm-pr-feedback F-03 (out-of-band swarm-pr-review run pr-1042-20261029):
// the identity gate in DraftAssignmentForm (templateState.owner vs the
// session principal) is LOAD-BEARING — useAuthStore.fetchMe can swap the
// user object in place (set({ user, isAuthenticated: true })) while
// isAuthenticated stays true and the form stays mounted — but the frozen
// C2 form test cannot detect the gate's removal: it drives login() inside
// act(), and act flushes the reload effect before the assertions, so a
// gate-less render is repaired before anything is asserted (the OOB
// reviewer's mutation probe removed the gate and both form files stayed
// green).
//
// This file closes that gap: each swap is applied inside flushSync with
// IS_REACT_ACT_ENVIRONMENT disabled, forcing the render synchronously while
// the reload effect stays deferred — the exact window the gate protects.
// Leg 1: a different principal must blank the template affordance in that
// window (a gate removal fails this leg — the previous principal's template
// is still in templateState). Leg 2: a fetchMe-style in-place swap of a NEW
// user object with the SAME principal id is NOT an identity switch (the
// gate compares ids) — the affordance must survive (an object-identity
// mutation fails this leg). A post-flush assertion completes each leg after
// the reload effect resolves.
//
// The lib is mocked at the principal-scoped call surface (same shape as the
// frozen C2 form test); transport-level modules are mocked identically.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { flushSync } from "react-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { Vault } from "@/lib/api";

const { mockPostFn, mockGetFn, mockPatchFn, mockResetCsrfToken, mockEnsureCsrfToken, mockResetSubpathRefreshDiagnostic, mockResetCitationReport, mockUploadDocument, mockListAccessibleVaults } = vi.hoisted(() => ({
  mockPostFn: vi.fn(),
  mockGetFn: vi.fn(),
  mockPatchFn: vi.fn(),
  mockResetCsrfToken: vi.fn(),
  mockEnsureCsrfToken: vi.fn().mockResolvedValue("mock-csrf-token"),
  mockResetSubpathRefreshDiagnostic: vi.fn(),
  mockResetCitationReport: vi.fn(),
  mockUploadDocument: vi.fn(),
  mockListAccessibleVaults: vi.fn().mockResolvedValue({ vaults: [] as Vault[] }),
}));

const { mockJwtTokenHolder } = vi.hoisted(() => ({
  mockJwtTokenHolder: { value: null as string | null },
}));

vi.mock("axios", () => ({
  default: {
    create: vi.fn(() => ({
      get: mockGetFn,
      post: mockPostFn,
      patch: mockPatchFn,
      put: vi.fn(),
      delete: vi.fn(),
      interceptors: {
        request: { use: vi.fn((cb) => cb) },
        response: { use: vi.fn((cb) => cb) },
      },
    })),
  },
}));

vi.mock("@/lib/api/core", () => ({
  getJwtAccessToken: vi.fn(() => mockJwtTokenHolder.value),
  onJwtAccessTokenPublished: vi.fn(() => () => {}),
}));

vi.mock("@/lib/api", () => ({
  API_BASE_URL: "/api",
  setJwtAccessToken: vi.fn((token: string | null) => {
    mockJwtTokenHolder.value = token;
  }),
  getJwtAccessToken: vi.fn(() => mockJwtTokenHolder.value),
  refreshAccessToken: vi.fn(),
  resetCsrfToken: mockResetCsrfToken,
  ensureCsrfToken: mockEnsureCsrfToken,
  ensureCsrfTokenPhysical: mockEnsureCsrfToken,
  resetSubpathRefreshDiagnostic: mockResetSubpathRefreshDiagnostic,
  attachCsrfInterceptor: vi.fn(),
  uploadDocument: mockUploadDocument,
  listAccessibleVaults: mockListAccessibleVaults,
  default: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    interceptors: {
      request: { use: vi.fn() },
      response: { use: vi.fn() },
    },
  },
}));

vi.mock("@/lib/api/onboarding", () => ({
  resetCitationReport: mockResetCitationReport,
  getOnboardingMilestones: vi.fn(),
  markCitationOpened: vi.fn(),
  dismissChecklist: vi.fn(),
}));

vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: {
    getState: vi.fn(() => ({ fetchVaults: vi.fn() })),
  },
}));

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    warning: vi.fn(),
  },
}));

vi.mock("@/components/ui/select", async () => {
  const React = await import("react");
  const SelectCtx = React.createContext<(v: string) => void>(() => {});

  function Select({
    onValueChange,
    children,
  }: {
    value?: string;
    onValueChange?: (v: string) => void;
    disabled?: boolean;
    children?: React.ReactNode;
  }) {
    return React.createElement(SelectCtx.Provider, { value: onValueChange ?? (() => {}) }, children);
  }
  function SelectTrigger({
    children,
    ...rest
  }: React.ButtonHTMLAttributes<HTMLButtonElement> & { id?: string }) {
    return React.createElement("button", { type: "button", ...rest }, children);
  }
  function SelectValue() {
    return null;
  }
  function SelectContent({ children }: { children?: React.ReactNode }) {
    return React.createElement("div", null, children);
  }
  function SelectItem({ value, children }: { value: string; children?: React.ReactNode }) {
    const onValueChange = React.useContext(SelectCtx);
    return React.createElement(
      "button",
      { type: "button", onClick: () => onValueChange(value) },
      children
    );
  }
  return {
    Select,
    SelectTrigger,
    SelectValue,
    SelectContent,
    SelectItem,
    SelectGroup: SelectContent,
    SelectLabel: SelectContent,
    SelectSeparator: () => null,
  };
});

const { mockTemplateList } = vi.hoisted(() => ({
  mockTemplateList: vi.fn(),
}));

vi.mock("@/lib/draftBriefTemplates", () => ({
  DRAFT_BRIEF_TEMPLATES_KEY_PREFIX: "ragapp.v1.draft-brief-templates:",
  DRAFT_BRIEF_TEMPLATES_STORAGE_KEY: "ragapp.v1.draft-brief-templates",
  clearAllDraftBriefTemplates: vi.fn(() => 0),
  resetDraftBriefTemplatesCacheForTests: vi.fn(),
  listDraftBriefTemplates: mockTemplateList,
  saveDraftBriefTemplate: mockTemplateList,
  latestDraftBriefTemplate: vi.fn(() => null),
}));

import { useAuthStore, resetInitState } from "@/stores/useAuthStore";
import {
  DraftAssignmentForm,
  createDefaultDraftAssignmentFormValue,
} from "./DraftAssignmentForm";

const USER_A = { id: 11, username: "usera", full_name: "User A", role: "admin" as const, is_active: true };
const USER_B = { id: 22, username: "userb", full_name: "User B", role: "admin" as const, is_active: true };

const TEMPLATE_NAME = "User A's form template";
const TEMPLATE = { id: "t-1", name: TEMPLATE_NAME, brief: {} };

function principalState(user: typeof USER_A | typeof USER_B | null) {
  return {
    user,
    accessToken: user ? `jwt-${user.id}` : null,
    isAuthenticated: user != null,
    isInitialized: true,
    initializationFailed: false,
    isLoading: false,
    needsSetup: false,
    authMode: "jwt" as const,
  };
}

function renderForm() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <DraftAssignmentForm
        value={createDefaultDraftAssignmentFormValue()}
        onChange={vi.fn()}
        idPrefix="identity-gate"
      />
    </QueryClientProvider>
  );
}

function apply_swap_outside_act(update: () => void) {
  const globalFlags = globalThis as Record<string, unknown>;
  const previous = globalFlags.IS_REACT_ACT_ENVIRONMENT;
  try {
    globalFlags.IS_REACT_ACT_ENVIRONMENT = false;
    flushSync(update);
  } finally {
    globalFlags.IS_REACT_ACT_ENVIRONMENT = previous ?? true;
  }
}

function applyButtonIn(container: HTMLElement) {
  return Array.from(container.querySelectorAll("button")).find((b) =>
    (b.textContent ?? "").includes(TEMPLATE_NAME)
  );
}

describe("DraftAssignmentForm identity gate — synchronous principal swaps (swarm-pr-feedback F-03)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetFn.mockReset();
    mockPostFn.mockReset();
    mockListAccessibleVaults.mockResolvedValue({ vaults: [] });
    mockEnsureCsrfToken.mockResolvedValue("mock-csrf-token");
    mockJwtTokenHolder.value = null;
    resetInitState();
    useAuthStore.setState(principalState(null));
    // Principal A owns one template; any other principal resolves none.
    mockTemplateList.mockImplementation((principalId: number) =>
      principalId === USER_A.id ? [TEMPLATE] : []
    );
  });

  afterEach(() => {
    resetInitState();
  });

  it("a principal swap outside act blanks the template affordance SYNCHRONOUSLY, before the reload effect", async () => {
    useAuthStore.setState(principalState(USER_A));
    const { container } = renderForm();

    // Non-vacuity: under principal A the affordance is rendered.
    expect(
      screen.getByRole("button", { name: `Apply template (${TEMPLATE_NAME})` })
    ).toBeTruthy();

    // The gate must blank the list in the forced-render window: with the
    // gate removed, the previous principal's template is still in
    // templateState and this assertion fails.
    apply_swap_outside_act(() => useAuthStore.setState(principalState(USER_B)));
    expect(applyButtonIn(container)).toBeUndefined();

    // After the reload effect flushes, B's list resolves to the same empty
    // result.
    await act(async () => {});
    expect(
      screen.queryByRole("button", { name: `Apply template (${TEMPLATE_NAME})` })
    ).toBeNull();
  });

  it("a fetchMe-style in-place swap of a NEW user object with the SAME principal id retains the template", async () => {
    useAuthStore.setState(principalState(USER_A));
    const { container } = renderForm();
    expect(
      screen.getByRole("button", { name: `Apply template (${TEMPLATE_NAME})` })
    ).toBeTruthy();

    // The gate compares principal IDs, not object identity: a fresh user
    // object with the same id is NOT an identity switch, so the affordance
    // survives the forced re-render. (A gate mutated to compare object
    // identity would blank it and fail this leg.)
    apply_swap_outside_act(() => useAuthStore.setState({ user: { ...USER_A } }));
    expect(applyButtonIn(container)).toBeTruthy();

    // The reload effect early-returns for a same-principal swap; the
    // template is still offered after the flush.
    await act(async () => {});
    expect(
      screen.getByRole("button", { name: `Apply template (${TEMPLATE_NAME})` })
    ).toBeTruthy();
  });
});
