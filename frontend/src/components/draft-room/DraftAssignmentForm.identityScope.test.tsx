// Issue #1036 C2 (plan-critic round 2, R2-F4): the Draft Room assignment
// form must not render or apply the previous principal's brief templates
// after an identity switch in the same tab.
//
// DraftAssignmentForm reads its template list once per mount
// (listDraftBriefTemplates()) and refreshes it only from its own saves, so
// at the base tree a different-principal login while the form stays
// mounted leaves user A's template rendered and applicable for user B.
// The final design makes the form track which principal its local template
// array belongs to: when the current principal differs it renders and
// applies nothing and reloads under the new principal.
//
// The different-principal leg asserts on the form's RENDERED template
// affordance (the "Apply template (name)" button), so it cannot pass
// vacuously; the same-principal leg pins boundary discrimination (it
// passes at the pre-fix base and must keep passing).
//
// The real form, real auth store, and real template lib are exercised;
// only transport-level modules are mocked (the auth transport scaffold
// from useAuthStore.identityScope.draftRoomUi.test.ts plus
// listAccessibleVaults and the jsdom Radix-Select stand-in from
// DraftAssignmentForm.test.tsx).

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { Vault } from "@/lib/api";
import type { DraftBrief } from "@/lib/api/draftRoom";

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

// Radix Select cannot be driven in jsdom (no pointer-capture /
// scrollIntoView). Stand in with a plain context-backed mock per the
// repo's testing gotchas doc (copied from DraftAssignmentForm.test.tsx).
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

import { useAuthStore, resetInitState } from "@/stores/useAuthStore";
import {
  DraftAssignmentForm,
  createDefaultDraftAssignmentFormValue,
} from "./DraftAssignmentForm";
import { resetDraftBriefTemplatesCacheForTests } from "@/lib/draftBriefTemplates";
import * as templatesModule from "@/lib/draftBriefTemplates";

// The fixed lib's principal-scoped call surface (extra trailing
// principalId; ignored by the 2-argument base implementation — see
// draftBriefTemplates.identityScope.test.ts).
type PrincipalScopedTemplateLib = {
  saveDraftBriefTemplate: (
    name: string,
    brief: DraftBrief,
    principalId: number
  ) => unknown[];
  listDraftBriefTemplates: (principalId: number) => { name: string }[];
};
const templatesLib = templatesModule as unknown as PrincipalScopedTemplateLib;

const USER_A = { id: 11, username: "usera", full_name: "User A", role: "admin" as const, is_active: true };
const USER_B = { id: 22, username: "userb", full_name: "User B", role: "admin" as const, is_active: true };

const TEMPLATE_NAME = "User A's form template";

const USER_A_BRIEF: DraftBrief = {
  piece_type: "memo",
  audience: "User A's private audience",
  purpose: "persuade",
  tone: "formal",
  target_words: 800,
  transformation_strength: "moderate",
  primary_input_id: null,
  must_include: ["user A's must-include phrase"],
  must_avoid: ["user A's must-avoid phrase"],
  preserve_quotes: true,
  preserve_numbers: true,
  preserve_uncertainty: false,
  drafting_priority: "accuracy",
  additional_instructions: "user A's private instructions",
};

/** Functional in-memory localStorage (vi.fn mock lacks key/length iteration). */
function installLocalStorage() {
  const store = new Map<string, string>();
  const impl = {
    getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
    key: (i: number) => Array.from(store.keys())[i] ?? null,
    get length() {
      return store.size;
    },
  };
  Object.defineProperty(window, "localStorage", { value: impl, configurable: true });
  return store;
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
        idPrefix="identity-scope"
      />
    </QueryClientProvider>
  );
}

describe("DraftAssignmentForm identity scope — brief templates (issue #1036 C2)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetFn.mockReset();
    mockPostFn.mockReset();
    mockListAccessibleVaults.mockResolvedValue({ vaults: [] });
    mockEnsureCsrfToken.mockResolvedValue("mock-csrf-token");
    mockJwtTokenHolder.value = null;
    resetInitState();
    useAuthStore.setState({
      user: null,
      accessToken: null,
      isAuthenticated: false,
      isInitialized: false,
      initializationFailed: false,
      isLoading: false,
      needsSetup: false,
      authMode: "unknown",
    });
    resetDraftBriefTemplatesCacheForTests();
    installLocalStorage();
  });

  afterEach(() => {
    resetInitState();
    resetDraftBriefTemplatesCacheForTests();
  });

  it("different-principal login while mounted stops rendering/applying A's template", async () => {
    useAuthStore.setState({
      user: USER_A,
      accessToken: "jwt-a",
      isAuthenticated: true,
      authMode: "jwt",
    });
    templatesLib.saveDraftBriefTemplate(TEMPLATE_NAME, USER_A_BRIEF, USER_A.id);

    const { container } = renderForm();

    // Non-vacuity: under principal A the form DOES render the template
    // affordance (asserted on the rendered template name so this leg can
    // never pass vacuously).
    expect(
      screen.getByRole("button", { name: `Apply template (${TEMPLATE_NAME})` })
    ).toBeTruthy();

    mockPostFn.mockResolvedValueOnce({
      data: { access_token: "jwt-b", user: USER_B },
    });

    await act(async () => {
      await useAuthStore.getState().login("userb", "pw");
    });

    // The boundary really crossed.
    expect(useAuthStore.getState().user?.id).toBe(22);

    // Nothing of A's templates may remain rendered or applicable for B.
    expect(screen.queryByRole("button", { name: /Apply template/ })).toBeNull();
    expect(container.textContent ?? "").not.toContain(TEMPLATE_NAME);
  });

  it("same-principal re-login while mounted retains A's template (boundary discrimination)", async () => {
    useAuthStore.setState({
      user: USER_A,
      accessToken: "jwt-a",
      isAuthenticated: true,
      authMode: "jwt",
    });
    templatesLib.saveDraftBriefTemplate(TEMPLATE_NAME, USER_A_BRIEF, USER_A.id);

    renderForm();

    expect(
      screen.getByRole("button", { name: `Apply template (${TEMPLATE_NAME})` })
    ).toBeTruthy();

    mockPostFn.mockResolvedValueOnce({
      data: { access_token: "jwt-a2", user: USER_A },
    });

    await act(async () => {
      await useAuthStore.getState().login("usera", "pw");
    });

    expect(useAuthStore.getState().user?.id).toBe(11);
    expect(
      screen.getByRole("button", { name: `Apply template (${TEMPLATE_NAME})` })
    ).toBeTruthy();
  });
});
