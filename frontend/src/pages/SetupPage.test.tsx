import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import SetupPage from "./SetupPage";
import * as useAuthStoreModule from "@/stores/useAuthStore";
import { BrowserRouter } from "react-router-dom";

// Mock the dependencies
vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: vi.fn(),
}));

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual("react-router-dom");
  return {
    ...actual,
    useNavigate: vi.fn(() => vi.fn()),
  };
});

// Helper to render with router
const renderSetupPage = (needsSetup: boolean | null = true) => {
  vi.spyOn(useAuthStoreModule, "useAuthStore").mockReturnValue({
    register: vi.fn().mockResolvedValue({ success: true }),
    needsSetup,
    isLoading: false,
  } as any);

  return render(
    <BrowserRouter>
      <SetupPage />
    </BrowserRouter>
  );
};

describe("SetupPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders form with all 4 inputs", () => {
    renderSetupPage();

    // Check for all 4 form inputs
    expect(screen.getByLabelText(/Username/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Full name/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/^Password$/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Confirm Password/i)).toBeInTheDocument();
  });

  it("renders with correct input types", () => {
    renderSetupPage();

    expect(screen.getByPlaceholderText("Username (required)")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Full name (optional)")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Password (min 8 characters)")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Confirm password")).toBeInTheDocument();
  });

  it("shows loading spinner when isLoading is true", () => {
    vi.spyOn(useAuthStoreModule, "useAuthStore").mockReturnValue({
      register: vi.fn().mockResolvedValue({ success: true }),
      needsSetup: true,
      isLoading: true,
    } as any);

    render(
      <BrowserRouter>
        <SetupPage />
      </BrowserRouter>
    );

    // Button should show loading state
    expect(screen.getByRole("button", { name: /Creating Account/i })).toBeInTheDocument();
    expect(screen.getByText(/Creating Account/i)).toBeInTheDocument();
  });

  it("shows loading state while checking setup status", () => {
    vi.spyOn(useAuthStoreModule, "useAuthStore").mockReturnValue({
      register: vi.fn().mockResolvedValue({ success: true }),
      needsSetup: null, // Loading state
      isLoading: false,
    } as any);

    render(
      <BrowserRouter>
        <SetupPage />
      </BrowserRouter>
    );

    expect(screen.getByText("Verifying your instance")).toBeInTheDocument();
  });

  it("validates username minimum length", async () => {
    const user = userEvent.setup();
    renderSetupPage();

    // Fill username with too few characters
    await user.type(screen.getByPlaceholderText("Username (required)"), "ab"); // Less than 3 characters
    await user.type(screen.getByPlaceholderText("Password (min 8 characters)"), "password123");
    await user.type(screen.getByPlaceholderText("Confirm password"), "password123");

    const submitButton = screen.getByRole("button", { name: /Create Superadmin Account/i });
    await user.click(submitButton);

    await waitFor(() => {
      expect(screen.getByText("Username must be at least 3 characters")).toBeInTheDocument();
    });
  });

  it("validates password minimum length", async () => {
    const user = userEvent.setup();
    renderSetupPage();

    // Fill username but with short password
    await user.type(screen.getByPlaceholderText("Username (required)"), "testuser");
    await user.type(screen.getByPlaceholderText("Password (min 8 characters)"), "short"); // Less than 8 characters
    await user.type(screen.getByPlaceholderText("Confirm password"), "short");

    const submitButton = screen.getByRole("button", { name: /Create Superadmin Account/i });
    await user.click(submitButton);

    await waitFor(() => {
      expect(screen.getByText("Password must be at least 8 characters")).toBeInTheDocument();
    });
  });

  it("validates password confirmation match", async () => {
    const user = userEvent.setup();
    renderSetupPage();

    // Fill form with mismatched passwords
    await user.type(screen.getByPlaceholderText("Username (required)"), "testuser");
    await user.type(screen.getByPlaceholderText("Password (min 8 characters)"), "password123");
    await user.type(screen.getByPlaceholderText("Confirm password"), "differentpassword");

    const submitButton = screen.getByRole("button", { name: /Create Superadmin Account/i });
    await user.click(submitButton);

    await waitFor(() => {
      expect(screen.getByText("Passwords do not match")).toBeInTheDocument();
    });
  });

  it("calls register on valid submit", async () => {
    const registerMock = vi.fn().mockResolvedValue({ success: true });
    vi.spyOn(useAuthStoreModule, "useAuthStore").mockReturnValue({
      register: registerMock,
      needsSetup: true,
      isLoading: false,
    } as any);

    const user = userEvent.setup();
    render(
      <BrowserRouter>
        <SetupPage />
      </BrowserRouter>
    );

    // Fill valid form
    await user.type(screen.getByPlaceholderText("Username (required)"), "adminuser");
    await user.type(screen.getByPlaceholderText("Full name (optional)"), "Admin User");
    await user.type(screen.getByPlaceholderText("Password (min 8 characters)"), "securepass123");
    await user.type(screen.getByPlaceholderText("Confirm password"), "securepass123");

    const submitButton = screen.getByRole("button", { name: /Create Superadmin Account/i });
    await user.click(submitButton);

    await waitFor(() => {
      expect(registerMock).toHaveBeenCalledWith("adminuser", "securepass123", "Admin User");
    });
  });

  it("stays on the setup wizard after registration (issue #622 two-step flow)", async () => {
    const navigate = vi.fn();
    const { useNavigate } = await import("react-router-dom");
    vi.mocked(useNavigate).mockReturnValue(navigate);

    // Stateful store mock mirroring the REAL register() transition
    // (useAuthStore sets needsSetup: false after a successful register) —
    // the final-critic regression for the /login redirect race.
    let mockNeedsSetup: boolean | null = true;
    const registerMock = vi.fn().mockImplementation(async () => {
      mockNeedsSetup = false;
      return { success: true };
    });
    vi.spyOn(useAuthStoreModule, "useAuthStore").mockImplementation(
      () =>
        ({
          register: registerMock,
          needsSetup: mockNeedsSetup,
          isLoading: false,
        }) as any
    );

    const user = userEvent.setup();
    render(
      <BrowserRouter>
        <SetupPage />
      </BrowserRouter>
    );

    // Fill valid form
    await user.type(screen.getByPlaceholderText("Username (required)"), "newadmin");
    await user.type(screen.getByPlaceholderText("Password (min 8 characters)"), "password123");
    await user.type(screen.getByPlaceholderText("Confirm password"), "password123");

    const submitButton = screen.getByRole("button", { name: /Create Superadmin Account/i });
    await user.click(submitButton);

    // Issue #622: registration now leads to the model-endpoint wizard step,
    // not straight into the app — navigation happens on save/skip only, and
    // the needsSetup:false flip must NOT trigger the /login redirect here.
    // First-arg extraction (not toHaveBeenCalledWith) because the redirect
    // calls navigate("/login", { replace: true }) — an exact-arity matcher
    // would silently never match the two-arg call (reviewer Round 2 probe).
    await waitFor(() => {
      expect(screen.getByText("Configure Chat Models")).toBeInTheDocument();
    });
    const navigateTargets = navigate.mock.calls.map((call) => call[0]);
    expect(navigateTargets).not.toContain("/login");
    expect(navigateTargets).not.toContain("/");
  });

  it("redirects direct visits to /login when setup is already complete", async () => {
    // PRR-006: pins the direct-visit leg of the redirect (needsSetup=false at
    // mount, no registration in flight → registeringRef still false).
    const navigate = vi.fn();
    const { useNavigate } = await import("react-router-dom");
    vi.mocked(useNavigate).mockReturnValue(navigate);

    vi.spyOn(useAuthStoreModule, "useAuthStore").mockReturnValue({
      register: vi.fn(),
      needsSetup: false,
      isLoading: false,
    } as any);

    render(
      <BrowserRouter>
        <SetupPage />
      </BrowserRouter>
    );

    await waitFor(() => {
      const targets = navigate.mock.calls.map((call) => call[0]);
      expect(targets).toContain("/login");
    });
    // The wizard step must never mount for a post-setup visitor.
    expect(screen.queryByText("Configure Chat Models")).not.toBeInTheDocument();
  });

  it("shows Create Superadmin Account button text", () => {
    renderSetupPage();

    expect(screen.getByRole("button", { name: /Create Superadmin Account/i })).toBeInTheDocument();
  });

  it("has proper labels for accessibility", () => {
    renderSetupPage();

    // Check for accessible label associations
    const usernameLabel = screen.getByText("Username").closest("label");
    const passwordLabel = screen.getByText("Password").closest("label");
    const confirmLabel = screen.getByText("Confirm Password").closest("label");

    expect(usernameLabel).toBeTruthy();
    expect(passwordLabel).toBeTruthy();
    expect(confirmLabel).toBeTruthy();
  });

  it("clears error when user starts typing after validation failure", async () => {
    const user = userEvent.setup();
    renderSetupPage();

    // First fill with valid form so button is enabled
    await user.type(screen.getByPlaceholderText("Username (required)"), "ab"); // Invalid: too short
    await user.type(screen.getByPlaceholderText("Password (min 8 characters)"), "password123");
    await user.type(screen.getByPlaceholderText("Confirm password"), "password123");

    // Submit to trigger validation error
    const submitButton = screen.getByRole("button", { name: /Create Superadmin Account/i });
    await user.click(submitButton);

    await waitFor(() => {
      expect(screen.getByText("Username must be at least 3 characters")).toBeInTheDocument();
    });

    // Now fix the username
    const usernameInput = screen.getByPlaceholderText("Username (required)");
    await user.clear(usernameInput);
    await user.type(usernameInput, "validuser");

    // Error should be cleared
    expect(screen.queryByText("Username must be at least 3 characters")).not.toBeInTheDocument();
  });

  it("disables submit button when form is incomplete", () => {
    renderSetupPage();

    const submitButton = screen.getByRole("button", { name: /Create Superadmin Account/i });
    expect(submitButton).toBeDisabled();
  });

  it("enables submit button when form is complete", async () => {
    const user = userEvent.setup();
    renderSetupPage();

    // Fill form
    await user.type(screen.getByPlaceholderText("Username (required)"), "newadmin");
    await user.type(screen.getByPlaceholderText("Password (min 8 characters)"), "password123");
    await user.type(screen.getByPlaceholderText("Confirm password"), "password123");

    const submitButton = screen.getByRole("button", { name: /Create Superadmin Account/i });
    expect(submitButton).not.toBeDisabled();
  });

  it("renders with correct card title and description", () => {
    renderSetupPage();

    expect(screen.getByText("Initial Setup")).toBeInTheDocument();
    expect(screen.getByText("Create the first superadmin account to get started")).toBeInTheDocument();
  });

  it("does not show password requirements when password is valid length", async () => {
    const user = userEvent.setup();
    renderSetupPage();

    await user.type(screen.getByPlaceholderText("Username (required)"), "testuser");
    await user.type(screen.getByPlaceholderText("Password (min 8 characters)"), "password123!"); // 12 chars
    await user.type(screen.getByPlaceholderText("Confirm password"), "password123!");

    const submitButton = screen.getByRole("button", { name: /Create Superadmin Account/i });
    await user.click(submitButton);

    // No password length error should appear
    expect(screen.queryByText("Password must be at least 8 characters")).not.toBeInTheDocument();
  });

  it("accepts optional full name", async () => {
    const registerMock = vi.fn().mockResolvedValue({ success: true });
    vi.spyOn(useAuthStoreModule, "useAuthStore").mockReturnValue({
      register: registerMock,
      needsSetup: true,
      isLoading: false,
    } as any);

    const user = userEvent.setup();
    render(
      <BrowserRouter>
        <SetupPage />
      </BrowserRouter>
    );

    // Fill form without full name (optional)
    await user.type(screen.getByPlaceholderText("Username (required)"), "newadmin");
    await user.type(screen.getByPlaceholderText("Password (min 8 characters)"), "password123");
    await user.type(screen.getByPlaceholderText("Confirm password"), "password123");

    const submitButton = screen.getByRole("button", { name: /Create Superadmin Account/i });
    await user.click(submitButton);

    await waitFor(() => {
      expect(registerMock).toHaveBeenCalledWith("newadmin", "password123", undefined);
    });
  });

  it("handles registration error without crash", async () => {
    const registerMock = vi.fn().mockRejectedValue(new Error("Registration failed"));

    vi.spyOn(useAuthStoreModule, "useAuthStore").mockReturnValue({
      register: registerMock,
      needsSetup: true,
      isLoading: false,
    } as any);

    const user = userEvent.setup();
    render(
      <BrowserRouter>
        <SetupPage />
      </BrowserRouter>
    );

    // Fill valid form
    await user.type(screen.getByPlaceholderText("Username (required)"), "newadmin");
    await user.type(screen.getByPlaceholderText("Password (min 8 characters)"), "password123");
    await user.type(screen.getByPlaceholderText("Confirm password"), "password123");

    const submitButton = screen.getByRole("button", { name: /Create Superadmin Account/i });
    
    // Should not throw, error is caught
    await user.click(submitButton);
  });
});