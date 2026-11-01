/**
 * UploadDropzone K05 acceptance checks (issue #768 AC8-AC10).
 *
 *   AC8  (DISCRIMINATING) — once settings report the server's
 *        allowed_extensions, the dropzone copy must advertise those types,
 *        not a fixed "PDF, DOCX, TXT, MD". RED at base, GREEN after the fix.
 *
 *   AC9  (DISCRIMINATING) — react-dropzone must receive an accept filter
 *        covering the server allowlist, so unsupported files are rejected
 *        before upload. RED at base (no accept option), GREEN after the fix.
 *
 *   AC10 (DISCRIMINATING) — when the dropzone is disabled (no writable
 *        vault), its "Select a writable vault to upload" instruction must
 *        not sit inside an ancestor opacity class (the contrast finding's
 *        structural proxy; jsdom computes no contrast). RED at base,
 *        GREEN after the fix.
 *
 * The real settings store is seeded directly; react-dropzone is mocked to
 * capture the options object it is given (the capture pattern of
 * DocumentsPage.test.tsx's dropzone mock).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";
import { UploadDropzone } from "@/components/documents/UploadDropzone";
import { useSettingsStore } from "@/stores/useSettingsStore";
import type { SettingsResponse } from "@/lib/api";

const { dropzoneOptions } = vi.hoisted(() => ({
  dropzoneOptions: { current: null as Record<string, unknown> | null },
}));

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

// The server allowlist under test: it extends beyond the hardcoded copy
// (adds .csv/.html), so the checks discriminate between advertised-copy and
// the fixed fallback list.
const SERVER_ALLOWED_EXTENSIONS = [".pdf", ".docx", ".txt", ".md", ".csv", ".html"];

function seedServerAllowlist() {
  useSettingsStore.setState({
    settings: {
      max_file_size_mb: 100,
      allowed_extensions: SERVER_ALLOWED_EXTENSIONS,
    } as unknown as SettingsResponse,
  });
}

function renderDropzone(props: {
  hasSelectedVault: boolean;
  canWriteActiveVault: boolean;
  hasActiveVaultId: boolean;
}) {
  return render(
    <UploadDropzone
      hasSelectedVault={props.hasSelectedVault}
      canWriteActiveVault={props.canWriteActiveVault}
      hasActiveVaultId={props.hasActiveVaultId}
      onFiles={vi.fn()}
      onRejected={vi.fn()}
    />
  );
}

describe("UploadDropzone K05 acceptance checks (issue #768)", () => {
  beforeEach(() => {
    dropzoneOptions.current = null;
    useSettingsStore.setState({ settings: null });
  });

  afterEach(() => {
    // No store write here: RTL's cleanup may not have unmounted yet, and a
    // setState outside act() warns. beforeEach re-seeds before each test.
    vi.restoreAllMocks();
  });

  it("dropzone copy advertises the server allowlist", () => {
    seedServerAllowlist();

    const { container } = renderDropzone({
      hasSelectedVault: true,
      canWriteActiveVault: true,
      hasActiveVaultId: true,
    });

    // .csv is on the server allowlist but not in the hardcoded copy.
    // RED at base, GREEN after the fix.
    expect(/\bcsv\b/i.test(container.textContent ?? "")).toBe(true);
  });

  it("dropzone passes an accept filter covering the server allowlist", () => {
    seedServerAllowlist();

    renderDropzone({
      hasSelectedVault: true,
      canWriteActiveVault: true,
      hasActiveVaultId: true,
    });

    const options = dropzoneOptions.current;
    expect(options).toBeTruthy();
    const acceptValues = Object.values(
      (options?.accept ?? {}) as Record<string, unknown>
    ).flat() as unknown[];
    // RED at base (useDropzone receives no accept), GREEN after the fix.
    expect(acceptValues.includes(".csv")).toBe(true);
  });

  it("disabled dropzone instructions are not inside a dimmed ancestor", () => {
    renderDropzone({
      hasSelectedVault: false,
      canWriteActiveVault: false,
      hasActiveVaultId: false,
    });

    const instruction = screen.getByText("Select a writable vault to upload");
    let dimmedAncestors = 0;
    let element: HTMLElement | null = instruction;
    while (element) {
      if (/\bopacity-(?!100\b)\d+/.test(String(element.className))) {
        dimmedAncestors += 1;
      }
      element = element.parentElement;
    }
    // RED at base (the whole card carries opacity-60), GREEN after the fix.
    expect(dimmedAncestors).toBe(0);
  });
});
