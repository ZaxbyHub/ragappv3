// frontend/src/pages/ProfilePage.a11y-revoke-names.test.tsx
// Issue #1159 AC3 (frozen acceptance check C3) — when the profile page
// lists several sessions, each revoke control's accessible name identifies
// ITS session, including the duplicate-device-and-IP sibling case.
//
// At base every non-current session's revoke button announced the identical
// generic "Revoke session".
//
// Harness: the ProfilePage.ownership.issue775.test.tsx mock set (@/lib/api
// partial mock, sonner, TestModeContext, capturing Button stub with REAL
// aria-label pass-through, auth store). The Button mock forwards all props
// (aria-label included) to a real <button>, so accessible-name queries see
// production behavior.

import type { ButtonHTMLAttributes } from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ProfilePage from "@/pages/ProfilePage";
import { useAuthStore } from "@/stores/useAuthStore";
import { publishAuthPrincipal } from "@/lib/api/auth-lifecycle";

const api = vi.hoisted(() => ({
  changePassword: vi.fn(),
  listOrganizations: vi.fn(),
  listAccessibleVaults: vi.fn(),
  listSessions: vi.fn(),
  revokeAllSessions: vi.fn(),
  revokeSession: vi.fn(),
}));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  ...api,
}));
vi.mock("sonner", () => ({ toast }));
vi.mock("@/fixtures/TestModeContext", () => ({ useTestMode: () => false }));
vi.mock("@/components/ui/button", () => ({
  Button: ({ children, ...props }: ButtonHTMLAttributes<HTMLButtonElement>) => (
    <button {...props}>{children}</button>
  ),
}));

function user(id: number, fullName: string) {
  return { id, username: `user-${id}`, full_name: fullName, role: "member" as const, is_active: true };
}

// Three sessions: s1 and s2 are the duplicate-identity pair (identical
// user_agent AND ip_address — the plan-critic round-1 F2 case); s3 is the
// current session (explanatory label, disabled).
const DUPLICATE_UA = "Chrome on Windows";
const DUPLICATE_IP = "10.0.0.7";
const sessions = [
  { id: 101, user_id: 1, user_agent: DUPLICATE_UA, ip_address: DUPLICATE_IP, created_at: "2026-11-20T10:00:00Z", expires_at: "2027-01-01T00:00:00Z", is_current: false },
  { id: 102, user_id: 1, user_agent: DUPLICATE_UA, ip_address: DUPLICATE_IP, created_at: "2026-11-21T11:00:00Z", expires_at: "2027-01-01T00:00:00Z", is_current: false },
  { id: 103, user_id: 1, user_agent: "E2E Browser", ip_address: "127.0.0.1", created_at: "2026-11-22T12:00:00Z", expires_at: "2027-01-01T00:00:00Z", is_current: true },
];

describe("ProfilePage a11y revoke names (#1159 AC3)", () => {
  const originalStore = useAuthStore.getState();

  beforeEach(() => {
    vi.resetAllMocks();
    api.listOrganizations.mockResolvedValue([]);
    api.listAccessibleVaults.mockResolvedValue({ vaults: [] });
    api.listSessions.mockResolvedValue({ sessions });
    api.changePassword.mockResolvedValue(undefined);
    api.revokeAllSessions.mockResolvedValue(undefined);
    api.revokeSession.mockResolvedValue(undefined);
    useAuthStore.setState({
      ...originalStore,
      user: user(1, "A User"),
      accessToken: "token-a",
      isAuthenticated: true,
      isLoading: false,
      isInitialized: true,
    });
    publishAuthPrincipal({ id: 1, role: "member" });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    useAuthStore.setState(originalStore, true);
  });

  it("every revoke control's accessible name is unique and identifies its session", async () => {
    render(<ProfilePage />);
    await waitFor(() => expect(screen.getAllByText(DUPLICATE_UA).length).toBe(2));

    const names = sessions
      .filter((s) => !s.is_current)
      .map((s) => {
        const btn = screen.getByRole("button", { name: new RegExp(`^Revoke session ${s.id}: `) });
        return btn.getAttribute("aria-label") ?? "";
      });

    // Uniqueness including the duplicate-device-and-IP pair.
    expect(new Set(names).size).toBe(2);
    // Each name carries its session's id and the shared device identity.
    for (const s of sessions.filter((x) => !x.is_current)) {
      const name = screen.getByRole("button", { name: new RegExp(`^Revoke session ${s.id}: ${DUPLICATE_UA} \\(${DUPLICATE_IP}\\)$`) });
      expect(name).toBeInTheDocument();
    }
  });

  it("the duplicate-identity siblings remain distinguishable byte-for-byte", async () => {
    render(<ProfilePage />);
    await waitFor(() => expect(screen.getAllByText(DUPLICATE_UA).length).toBeGreaterThan(0));

    const s1 = screen.getByRole("button", { name: /^Revoke session 101: / }).getAttribute("aria-label");
    const s2 = screen.getByRole("button", { name: /^Revoke session 102: / }).getAttribute("aria-label");
    expect(s1).toBeTruthy();
    expect(s2).toBeTruthy();
    expect(s1).not.toBe(s2!);
    expect(s1).toContain("101");
    expect(s2).toContain("102");
  });

  it("clicking a revoke control revokes THAT session (name-to-action mapping)", async () => {
    render(<ProfilePage />);
    await waitFor(() => expect(screen.getByRole("button", { name: /^Revoke session 102: / })).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /^Revoke session 102: / }));
    await waitFor(() => expect(api.revokeSession).toHaveBeenCalledWith(102));
    expect(api.revokeSession).toHaveBeenCalledTimes(1);
  });

  it("the current session keeps its explanatory label (visible text contained)", async () => {
    render(<ProfilePage />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Revoke — current session cannot be revoked here" })).toBeInTheDocument()
    );
  });

  it("sessions missing device and IP data fall back to the named placeholders (feedback round F-010)", async () => {
    // Feedback round: the `|| "Unknown device"` / `|| "unknown IP"`
    // fallbacks at ProfilePage.tsx:499 were never exercised. A session row
    // with a null user_agent and ip_address must still produce an
    // identifying name (id present, placeholders in place).
    const sparseSessions = [
      { id: 201, user_id: 1, user_agent: null, ip_address: null, created_at: "2026-11-23T10:00:00Z", expires_at: "2027-01-01T00:00:00Z", is_current: false },
    ];
    api.listSessions.mockResolvedValue({ sessions: sparseSessions });
    render(<ProfilePage />);
    const btn = await screen.findByRole("button", { name: /^Revoke session 201: Unknown device \(unknown IP\)$/ });
    expect(btn).toBeInTheDocument();
    expect(btn.getAttribute("aria-label")).toBe("Revoke session 201: Unknown device (unknown IP)");
  });
});
