/**
 * UploadDropzone accept-contract checks (issue #768 AC8-AC9).
 *
 *   AC8 — advertised copy follows the server allowlist once loaded; the
 *         "PDF, DOCX, TXT, MD" string is only the pre-load fallback.
 *
 *   AC9 — acceptance TRUTH is `serverAcceptsFilename` on both drop paths of
 *         the real component, driven through the options object the real
 *         UploadDropzone hands react-dropzone:
 *           channel (a): attr-accepted files are re-checked (an
 *             octet-stream-typed file the wildcard MIME key admitted is
 *             refused by the predicate);
 *           channel (b): TYPE-ONLY attribute rejections the server would
 *             accept (extensionless names that become `.txt`) are RECOVERED
 *             and delivered through onFiles exactly once; size-only and
 *             mixed type+size rejections stay rejected.
 *
 * The unit block pins the server-mirroring predicate semantics (basename,
 * secure_filename sanitize, Python `Path.suffix` rules, `.txt` fallback,
 * lowercase membership) that both channels depend on.
 *
 * react-dropzone is mocked only to CAPTURE the options the real component
 * passes (the capture pattern of UploadDropzone.k05.test.tsx); the settings
 * store is the real store seeded directly.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act } from "@testing-library/react";
import "@testing-library/jest-dom";
import type { FileRejection } from "react-dropzone";
import { UploadDropzone } from "@/components/documents/UploadDropzone";
import { serverAcceptsFilename, uploadAcceptOption } from "@/lib/uploadLimits";
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

const SERVER_ALLOWED_EXTENSIONS = [".csv", ".txt", ".pdf"];

function seedSettings(
  allowedExtensions: string[],
  maxFileSizeMb = 100
): void {
  useSettingsStore.setState({
    settings: {
      max_file_size_mb: maxFileSizeMb,
      allowed_extensions: allowedExtensions,
    } as unknown as SettingsResponse,
  });
}

function makeFile(name: string, type = "text/plain", bytes = 8): File {
  return new File([new Uint8Array(bytes)], name, { type });
}

/** Build a react-dropzone rejection with the given error codes. */
function rejectionOf(
  file: File,
  errors: Array<{ code: string; message: string }>
): FileRejection {
  return { file, errors };
}

function renderDropzone(props?: {
  hasSelectedVault?: boolean;
  canWriteActiveVault?: boolean;
  hasActiveVaultId?: boolean;
  onFiles?: (files: File[]) => void;
  onRejected?: (names: string[]) => void;
}) {
  return render(
    <UploadDropzone
      hasSelectedVault={props?.hasSelectedVault ?? true}
      canWriteActiveVault={props?.canWriteActiveVault ?? true}
      hasActiveVaultId={props?.hasActiveVaultId ?? true}
      onFiles={props?.onFiles ?? vi.fn()}
      onRejected={props?.onRejected ?? vi.fn()}
    />
  );
}

/** Drive the captured onDrop of the last-mounted real component. */
async function drop(
  accepted: File[],
  rejections: FileRejection[]
): Promise<void> {
  const onDrop = dropzoneOptions.current?.onDrop as
    | ((a: File[], r: FileRejection[]) => void)
    | undefined;
  expect(onDrop).toBeTruthy();
  await act(async () => {
    onDrop?.(accepted, rejections);
  });
}

describe("serverAcceptsFilename mirrors the server pipeline", () => {
  it("extensionless names fall back to .txt: allowed when .txt is listed, refused otherwise", () => {
    expect(serverAcceptsFilename("document", [".txt"])).toBe(true);
    expect(serverAcceptsFilename("document", [".pdf", ".csv"])).toBe(false);
  });

  it("hidden dotfiles have no suffix (Python Path.suffix) and take the .txt fallback", () => {
    expect(serverAcceptsFilename(".pdf", [".txt"])).toBe(true);
    expect(serverAcceptsFilename(".pdf", [".pdf"])).toBe(false);
  });

  it("a trailing dot is not a suffix: the .txt fallback applies", () => {
    expect(serverAcceptsFilename("name.", [".txt"])).toBe(true);
  });

  it("only the LAST suffix counts (archive.tar.gz has suffix .gz)", () => {
    expect(serverAcceptsFilename("archive.tar.gz", [".gz"])).toBe(true);
    expect(serverAcceptsFilename("archive.tar.gz", [".tar.gz"])).toBe(false);
  });

  it("a trailing space sanitizes to an underscore, so the suffix becomes .pdf_ — mirroring the server's rejection", () => {
    // Server pipeline: secure_filename("file.pdf ") == "file.pdf_";
    // Path("file.pdf_").suffix == ".pdf_", which is NOT in [".pdf"] — the
    // server rejects it, so the client predicate must agree (refuse).
    expect(serverAcceptsFilename("file.pdf ", [".pdf"])).toBe(false);
  });

  it("suffix is lowercased but the CONFIGURED set is raw (server mirror, PRR-004)", () => {
    // Server: Path(name).suffix.lower() in settings.allowed_extensions — the
    // suffix is lowercased, the configured entries are NOT normalized.
    expect(serverAcceptsFilename("DATA.PDF", [".pdf"])).toBe(true);
    expect(serverAcceptsFilename("DATA.PDF", [".PDF"])).toBe(false); // raw set keeps ".PDF"
    expect(serverAcceptsFilename("notes.TXT", [".txt", ".pdf"])).toBe(true);
    expect(serverAcceptsFilename("notes.txt", ["pdf"])).toBe(false); // dotless entry never matches
  });

  it("near-miss extensions are refused (.htm is not .html)", () => {
    expect(serverAcceptsFilename("evil.htm", [".html"])).toBe(false);
  });

  it("an unlisted extension is refused and an empty allowlist refuses everything", () => {
    expect(serverAcceptsFilename("mystery.bin", [".pdf", ".txt"])).toBe(false);
    expect(serverAcceptsFilename("mystery.bin", [])).toBe(false);
    expect(serverAcceptsFilename("report.pdf", [])).toBe(false);
  });

  it("basename uses POSIX semantics (split on / only, PRR-003) — agreeing with the server on backslash names", () => {
    // POSIX os.path.basename leaves backslashes in the name; secure_filename
    // then strips them. Both sides must reach the SAME suffix decision.
    expect(serverAcceptsFilename("/tmp/notes.csv", [".csv"])).toBe(true);
    expect(serverAcceptsFilename("a" + String.fromCharCode(92) + ".pdf", [".pdf"])).toBe(true); // both -> "a.pdf" -> .pdf
    expect(serverAcceptsFilename("report.pdf" + String.fromCharCode(92) + "notes", [".pdf"])).toBe(false); // both -> suffix ".pdfnotes"
    expect(serverAcceptsFilename("report.pdf" + String.fromCharCode(92) + "notes", [".txt"])).toBe(false); // suffix ".pdfnotes" is non-empty: NO .txt fallback on either side
  });

  it("a name that sanitizes to EMPTY follows the server's unnamed_file.txt fallback (K-16)", () => {
    // Server: secure_filename("") -> "" -> "unnamed_file.txt" -> suffix .txt.
    expect(serverAcceptsFilename("文档", [".txt"])).toBe(true);
    expect(serverAcceptsFilename("文档", [".pdf"])).toBe(false);
    expect(serverAcceptsFilename("", [".txt"])).toBe(true);
  });
});

describe("uploadAcceptOption builds the advisory accept attribute", () => {
  it("returns undefined for an empty allowlist", () => {
    expect(uploadAcceptOption([])).toBeUndefined();
  });

  it("carries the normalized, deduplicated, sorted extensions under one valid MIME key", () => {
    expect(uploadAcceptOption(["pdf", ".CSV", ".txt", ".txt"])).toEqual({
      "application/octet-stream": [".csv", ".pdf", ".txt"],
    });
  });
});

describe("UploadDropzone drop acceptance contract (issue #768 AC9)", () => {
  beforeEach(() => {
    dropzoneOptions.current = null;
    useSettingsStore.setState({ settings: null });
  });

  afterEach(() => {
    // No store write here: RTL's cleanup may not have unmounted yet, and a
    // setState outside act() warns. beforeEach re-seeds before each test.
    vi.restoreAllMocks();
  });

  it("recovers a type-only rejection the server would accept and refuses the predicate miss, each exactly once", async () => {
    seedSettings(SERVER_ALLOWED_EXTENSIONS);
    const onFiles = vi.fn();
    const onRejected = vi.fn();
    renderDropzone({ onFiles, onRejected });

    const goodPdf = makeFile("good.pdf", "application/pdf");
    const evilHtm = makeFile("evil.htm", "text/html");
    const extensionless = makeFile("document", "");

    // The realistic attr split: good.pdf accepted by the attribute;
    // evil.htm and the extensionless name rejected on TYPE ONLY.
    await drop([goodPdf], [
      rejectionOf(evilHtm, [
        { code: "file-invalid-type", message: "File type must be .csv, .txt, .pdf" },
      ]),
      rejectionOf(extensionless, [
        { code: "file-invalid-type", message: "File type must be .csv, .txt, .pdf" },
      ]),
    ]);

    // Recovery: the extensionless name (which the server would store as
    // .txt) is delivered through onFiles TOGETHER with the attr-accepted
    // pdf — one call, in that order.
    expect(onFiles).toHaveBeenCalledTimes(1);
    const delivered = onFiles.mock.calls[0][0];
    expect(delivered.map((f: File) => f.name)).toEqual(["good.pdf", "document"]);
    expect(delivered[0]).toBe(goodPdf);
    expect(delivered[1]).toBe(extensionless);

    // The true type miss is reported once, naming the file. It arrived as a
    // rejection, so its message carries the attribute's own reason (the
    // "file type not allowed" wording is reserved for channel (a) files).
    expect(onRejected).toHaveBeenCalledTimes(1);
    const rejectedNames = onRejected.mock.calls[0][0];
    expect(rejectedNames).toHaveLength(1);
    expect(rejectedNames[0]).toContain("evil.htm");
    expect(rejectedNames[0]).toContain("File type must be .csv, .txt, .pdf");
  });

  it("refuses an attr-accepted octet-stream file whose extension the server rejects", async () => {
    seedSettings(SERVER_ALLOWED_EXTENSIONS);
    const onFiles = vi.fn();
    const onRejected = vi.fn();
    renderDropzone({ onFiles, onRejected });

    // The wildcard MIME key admits this by attribute; the predicate must
    // refuse it (channel a).
    const mysteryBin = makeFile("mystery.bin", "application/octet-stream");
    await drop([mysteryBin], []);

    expect(onFiles).not.toHaveBeenCalled();
    expect(onRejected).toHaveBeenCalledTimes(1);
    expect(onRejected.mock.calls[0][0]).toHaveLength(1);
    expect(onRejected.mock.calls[0][0][0]).toContain("mystery.bin");
    expect(onRejected.mock.calls[0][0][0]).toContain("file type not allowed");
  });

  it("keeps a mixed type+size rejection rejected (the size contract is not recoverable)", async () => {
    seedSettings(SERVER_ALLOWED_EXTENSIONS);
    const onFiles = vi.fn();
    const onRejected = vi.fn();
    renderDropzone({ onFiles, onRejected });

    const oversized = makeFile("huge.pdf", "application/pdf");
    await drop([], [
      rejectionOf(oversized, [
        { code: "file-invalid-type", message: "File type must be .csv, .txt, .pdf" },
        { code: "file-too-large", message: "File is larger than 100 MB" },
      ]),
    ]);

    expect(onFiles).not.toHaveBeenCalled();
    expect(onRejected).toHaveBeenCalledTimes(1);
    const names = onRejected.mock.calls[0][0];
    expect(names).toHaveLength(1);
    expect(names[0]).toContain("huge.pdf");
    // Both attribute reasons survive in the reported message.
    expect(names[0]).toContain("File type must be .csv, .txt, .pdf");
    expect(names[0]).toContain("File is larger than 100 MB");
  });

  it("keeps a size-only rejection rejected even though the filename passes the predicate", async () => {
    seedSettings(SERVER_ALLOWED_EXTENSIONS);
    const onFiles = vi.fn();
    const onRejected = vi.fn();
    renderDropzone({ onFiles, onRejected });

    const oversized = makeFile("big.csv", "text/csv");
    await drop([], [
      rejectionOf(oversized, [
        { code: "file-too-large", message: "File is larger than 100 MB" },
      ]),
    ]);

    expect(onFiles).not.toHaveBeenCalled();
    expect(onRejected).toHaveBeenCalledTimes(1);
    expect(onRejected.mock.calls[0][0][0]).toContain("big.csv");
  });

  it("advertises the server allowlist in copy once settings are loaded", () => {
    seedSettings([".csv", ".html", ".pdf"]);
    const { container } = renderDropzone();

    expect(container.textContent ?? "").toMatch(/Supports CSV, HTML, PDF files/);
  });

  it("keeps the hardcoded copy only before settings load", () => {
    const { container } = renderDropzone();

    expect(container.textContent ?? "").toMatch(
      /Supports PDF, DOCX, TXT, MD files/
    );
  });

  it("renders the empty-allowlist truth instead of malformed copy (PRR-007 render-level)", () => {
    seedSettings([]);

    renderDropzone();

    // The fallback copy is skipped (an empty array is truthy), so the render
    // must carry the truth sentence — Multi-Stage required a render-level pin.
    expect(screen.getByText(/no file types are currently allowed/i)).toBeInTheDocument();
    expect(screen.queryByText(/Supports PDF, DOCX, TXT, MD files/)).toBeNull();
  });

  it("falls back to the client limit when the server reports a non-positive max (PRR-029)", () => {
    // config.py has no ge=1 validator on max_file_size_mb, so 0 is reachable
    // from the environment; the dropzone must mirror the page's positivity
    // guard instead of setting maxSize 0 ("Max 0 MB" + reject-everything).
    seedSettings(SERVER_ALLOWED_EXTENSIONS, 0);

    renderDropzone();

    expect(screen.getByText(/Max 100 MB/)).toBeInTheDocument();
  });

  it("accept option is exactly one valid MIME key carrying the extension list (K-39: real-hook shape)", () => {
    seedSettings(SERVER_ALLOWED_EXTENSIONS);

    renderDropzone();

    // The advisory accept must be shaped so react-dropzone's
    // pickerOptionsFromAccept accepts the key (a valid MIME type — otherwise
    // the real hook console.warns "Skipped ... not a valid MIME type" at
    // mount; asserted here on the captured option because this suite drives
    // the hook through a mock, where the library warning cannot fire).
    const accept = dropzoneOptions.current?.accept as Record<
      string,
      string[]
    >;
    expect(accept).toBeTruthy();
    const keys = Object.keys(accept);
    expect(keys).toEqual(["application/octet-stream"]);
    expect(keys[0]).toMatch(/^[-\w.]+\/([-\w.]+|\*)$/);
  });

  it("dims no ancestor of the disabled instruction (only the icon affordance dims)", () => {
    renderDropzone({
      hasSelectedVault: false,
      canWriteActiveVault: false,
      hasActiveVaultId: false,
    });

    const instruction = screen.getByText("Select a writable vault to upload");
    let dimmedAncestors = 0;
    let element: HTMLElement | null = instruction;
    while (element) {
      // Matches opacity-<n> (n != 100) and opacity-[arbitrary] Tailwind
      // values (K-45: the digit-only form missed arbitrary-value classes).
      if (new RegExp("opacity-(?!100\\b)(\\d+|\\[[^\\]]+\\])").test(String(element.className))) {
        dimmedAncestors += 1;
      }
      element = element.parentElement;
    }
    expect(dimmedAncestors).toBe(0);
  });
});
