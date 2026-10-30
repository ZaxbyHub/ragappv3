/**
 * PageShell K05 INTEGRATION checks (issue #768 AC7): the shell's settings
 * fetch is the transport that carries the server's upload limits to the
 * upload surface, end-to-end and safely.
 *
 *   (a) integrated path — after the shell's getSettings resolves, a REAL
 *       UploadDropzone mounted inside the shell (same real settings store,
 *       no reseeding) advertises the server allowlist, shows the server's
 *       max badge, and accepts a drop of an allowed file;
 *   (b) deferred-response race — a store already populated by the Settings
 *       page is never clobbered by a late shell response (fill-only guard);
 *   (c) failed fetch — the store stays null ("unloaded") and no
 *       unconfigured-chat banner appears (fail-safe);
 *   (d) unmount-then-resolve — a response landing after unmount writes
 *       nothing and raises no state-update error.
 *
 * Harness mirrors PageShell.k05.test.tsx / PageShell.test.tsx (proven in
 * this repo); react-dropzone is mocked only to capture the options the real
 * UploadDropzone passes, so drops can be driven.
 */
import type { HTMLAttributes, ReactNode } from "react";
import { render, screen, waitFor, act } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import type { FileRejection } from "react-dropzone";
import { PageShell } from "./PageShell";
import { UploadDropzone } from "@/components/documents/UploadDropzone";
import { useSettingsStore } from "@/stores/useSettingsStore";
import { mockSettings } from "@/fixtures/settings";

const { mockGetSettings, dropzoneOptions } = vi.hoisted(() => ({
  mockGetSettings: vi.fn(),
  dropzoneOptions: { current: null as Record<string, unknown> | null },
}));

vi.mock("@/lib/api", () => ({
  getSettings: mockGetSettings,
}));

// Issue #782: PageShell now mounts the FirstRunChecklist; keep its
// fetch deterministic here (server says hidden).
vi.mock("@/lib/api/onboarding", () => ({
  getOnboardingMilestones: vi.fn().mockResolvedValue({
    vault_created: true,
    upload_indexed: true,
    first_question_asked: true,
    first_citation_opened: true,
    show_checklist: false,
  }),
  markCitationOpened: vi.fn(),
  dismissChecklist: vi.fn(),
}));

vi.mock("./Navigation", () => ({
  Navigation: () => <nav aria-label="Primary navigation" />,
}));

vi.mock("@/components/shared/UploadIndicator", () => ({
  UploadIndicator: () => null,
}));

vi.mock("framer-motion", () => ({
  AnimatePresence: ({ children }: { children: ReactNode }) => <>{children}</>,
  motion: {
    div: ({
      children,
      variants: _variants,
      initial: _initial,
      animate: _animate,
      exit: _exit,
      transition: _transition,
      ...props
    }: HTMLAttributes<HTMLDivElement> & Record<string, unknown>) => (
      <div {...props}>{children}</div>
    ),
  },
  useReducedMotion: () => true,
}));

// Capture the options the REAL UploadDropzone hands react-dropzone so a
// drop can be driven against the live component.
vi.mock("react-dropzone", () => ({
  useDropzone: vi.fn((options: Record<string, unknown>) => {
    dropzoneOptions.current = options;
    return {
      getRootProps: () => ({ role: "button" }),
      getInputProps: () => ({ type: "file" }),
      isDragActive: false,
    };
  }),
}));

const shellProps = {
  activeItem: "documents" as const,
  onItemSelect: vi.fn(),
  healthStatus: {
    backend: true,
    embeddings: true,
    chat: true,
    loading: false,
    lastChecked: null,
  },
};

function renderShell(children?: ReactNode) {
  return render(
    <MemoryRouter initialEntries={["/documents"]}>
      <PageShell {...shellProps}>
        {children ?? <button type="button">Page content</button>}
      </PageShell>
    </MemoryRouter>
  );
}

async function flushMicrotasks(rounds = 10) {
  for (let i = 0; i < rounds; i++) await Promise.resolve();
}

describe("PageShell K05 settings propagation, integrated (issue #768)", () => {
  beforeEach(() => {
    mockGetSettings.mockReset();
    dropzoneOptions.current = null;
    sessionStorage.removeItem("unconfigured-chat-banner-dismissed");
    useSettingsStore.getState().resetState();
  });

  afterEach(() => {
    useSettingsStore.getState().resetState();
  });

  it("shell settings fetch feeds a real in-shell UploadDropzone: allowlist copy, 250 MB badge, accepted .csv drop", async () => {
    mockGetSettings.mockResolvedValue({
      ...mockSettings,
      chat_configured: true,
      max_file_size_mb: 250,
      allowed_extensions: [".pdf", ".csv"],
    });
    const onFiles = vi.fn();
    const onRejected = vi.fn();

    // The upload surface rides INSIDE the shell, against the same real
    // store the shell populates — no reseeding.
    renderShell(
      <UploadDropzone
        hasSelectedVault
        canWriteActiveVault
        hasActiveVaultId
        onFiles={onFiles}
        onRejected={onRejected}
      />
    );

    // The shell request resolves inside act so the store write (and the
    // UploadDropzone re-render off it) is not an act() violation.
    await waitFor(() => {
      expect(mockGetSettings).toHaveBeenCalled();
    });
    await act(async () => {
      await flushMicrotasks();
    });

    // The store got the shell response...
    expect(useSettingsStore.getState().settings?.max_file_size_mb).toBe(250);
    // ...and the surface re-rendered off it: advertised types and badge.
    expect(screen.getByText(/Supports CSV, PDF files/)).toBeInTheDocument();
    expect(screen.getByText("Max 250 MB")).toBeInTheDocument();

    // A .csv drop (allowed by the server list) passes the predicate.
    const csv = new File([new Uint8Array(8)], "data.csv", { type: "text/csv" });
    const onDrop = dropzoneOptions.current?.onDrop as
      | ((accepted: File[], rejections: FileRejection[]) => void)
      | undefined;
    expect(onDrop).toBeTruthy();
    await act(async () => {
      onDrop?.([csv], []);
    });
    expect(onFiles).toHaveBeenCalledTimes(1);
    expect(onFiles.mock.calls[0][0].map((file: File) => file.name)).toEqual([
      "data.csv",
    ]);
    expect(onRejected).not.toHaveBeenCalled();
  });

  it("a late shell response never clobbers a store the Settings page already populated", async () => {
    let resolveShell: (value: { chat_configured: boolean; max_file_size_mb: number }) => void = () => {};
    mockGetSettings.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveShell = resolve;
        })
    );

    renderShell();

    // The Settings page wrote 50 MB while the shell request was in flight.
    await act(async () => {
      useSettingsStore.getState().setSettings({ ...mockSettings, max_file_size_mb: 50 });
    });

    // The shell response (250 MB) resolves afterwards — the fill-only guard
    // must keep the newer 50 MB.
    await act(async () => {
      resolveShell({ chat_configured: true, max_file_size_mb: 250 });
      await flushMicrotasks();
    });

    expect(useSettingsStore.getState().settings?.max_file_size_mb).toBe(50);
  });

  it("a failed shell fetch writes nothing to the store and shows no unconfigured-chat banner", async () => {
    mockGetSettings.mockRejectedValue(new Error("network down"));

    renderShell();

    await waitFor(() => {
      expect(mockGetSettings).toHaveBeenCalled();
    });
    await act(async () => {
      await flushMicrotasks();
    });

    // Store keeps the null "unloaded" state (consumers use their fallbacks)
    // and the banner stays hidden (fail-safe, never nag).
    expect(useSettingsStore.getState().settings).toBeNull();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.getByText("Page content")).toBeInTheDocument();
  });

  it("a shell response landing after unmount writes nothing and raises no state error", async () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    let resolveShell: (value: { chat_configured: boolean; max_file_size_mb: number }) => void = () => {};
    mockGetSettings.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveShell = resolve;
        })
    );

    const { unmount } = renderShell();
    await act(async () => {
      unmount();
    });

    await act(async () => {
      resolveShell({ chat_configured: false, max_file_size_mb: 250 });
      await flushMicrotasks();
    });

    // The cancelled effect wrote neither the store nor component state.
    expect(useSettingsStore.getState().settings).toBeNull();
    const stateErrors = errorSpy.mock.calls.filter((args) =>
      /Warning: An update|not wrapped in act/i.test(String(args[0]))
    );
    expect(stateErrors).toHaveLength(0);
  });
});
