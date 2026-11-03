/**
 * Client-side upload guards and the shared upload-acceptance predicate.
 *
 * The effective limits are server-configured (`settings.max_file_size_mb` and
 * `settings.allowed_extensions`, exposed by GET /settings and held in
 * `useSettingsStore`); the constants below are only the FALLBACK used until
 * settings have loaded (or when a caller has no settings access). Helpers
 * accept the effective limit as an explicit argument so callers never read
 * the fallback directly.
 */
export const MAX_UPLOAD_FILE_SIZE_MB = 100;
export const DEFAULT_UPLOAD_LIMIT_BYTES = MAX_UPLOAD_FILE_SIZE_MB * 1024 * 1024;

export function formatUploadSizeLimit(maxFileSizeMb = MAX_UPLOAD_FILE_SIZE_MB): string {
  return `${maxFileSizeMb} MB`;
}

export function uploadSizeExceededMessage(
  fileName: string,
  maxFileSizeMb = MAX_UPLOAD_FILE_SIZE_MB
): string {
  return `${fileName} is too large. Max size: ${formatUploadSizeLimit(maxFileSizeMb)}.`;
}

export function isUploadTooLarge(
  file: File,
  maxFileSizeBytes = DEFAULT_UPLOAD_LIMIT_BYTES
): boolean {
  return file.size > maxFileSizeBytes;
}

/**
 * The single rule for turning a server-reported `max_file_size_mb` into the
 * limit the client enforces: use the server value whenever it is present,
 * fall back only when settings have not loaded.
 *
 * Home of this rule is deliberately here rather than in the upload store:
 * Draft Room is forbidden from importing `useUploadStore` (pinned by
 * DraftSourceUpload.test.tsx's architecture guard), but every upload surface
 * must resolve one server value the same way. Review PRR-004 found the Draft
 * Room gate had grown a third rule — a positivity guard that substituted the
 * 100 MB fallback for a server-reported 0 — so Draft Room admitted files the
 * server 413s while the Composer rejected them. A literal 0 is now applied
 * literally, matching the server, which computes
 * `max_file_size_mb * 1024 * 1024` with no fallback.
 */
export function resolveUploadLimitMb(
  serverMaxFileSizeMb: number | null | undefined
): number {
  return serverMaxFileSizeMb ?? MAX_UPLOAD_FILE_SIZE_MB;
}

export function normalizeUploadErrorMessage(
  error: unknown,
  maxFileSizeMb = MAX_UPLOAD_FILE_SIZE_MB
): string {
  const message = error instanceof Error ? error.message : "Upload failed";
  const status = typeof error === "object" && error !== null
    ? (error as { status?: number }).status
    : undefined;
  if (status === 413 || /request entity too large|payload too large|file too large/i.test(message)) {
    return `File too large. Max size: ${formatUploadSizeLimit(maxFileSizeMb)}.`;
  }
  return message;
}

/**
 * Normalize a server `allowed_extensions` entry: lowercase, dot-prefixed.
 */
export function normalizeAllowedExtension(ext: string): string {
  const trimmed = ext.trim().toLowerCase();
  return trimmed.startsWith(".") ? trimmed : `.${trimmed}`;
}

/**
 * Mirror of the server's filename pipeline for client-side acceptance
 * (issue #768 AC8-AC9; backend: upload_validation.secure_filename +
 * documents.py's extension check) — step for step.
 *
 * Allowlist shape (review PRR-003, correcting this docstring's earlier claim):
 * membership is tested against the entries **exactly as the caller supplies
 * them**. The server compares `Path(file_name).suffix.lower()` against the
 * RAW `settings.allowed_extensions`, so a caller that pre-normalizes (every
 * surface here does, following UploadDropzone's precedent) is trading strict
 * parity for a sane-config contract: an uppercase or dotless configured entry
 * is accepted client-side and rejected server-side. `allowed_extensions` is
 * env-only (`config.py` BaseSettings; not mutable through the settings API),
 * so this is an operator misconfiguration case, disclosed in the release
 * notes rather than silently claimed as full parity.
 *
 * 1. basename with POSIX semantics: split on "/" only (PRR-003: the server's
 *    os.path.basename on Linux treats backslash as an ordinary character;
 *    the previous both-separator split accepted backslash names the server
 *    rejects — moot for browser File.name, but the mirror must not diverge)
 * 2. spaces -> underscores, ASCII-only, keep [A-Za-z0-9._-] (secure_filename)
 * 3. final suffix with Python Path.suffix semantics: the last dot only
 *    when it is at index > 0 AND before the last character (a no-dot name, a
 *    leading-dot-only name (dotfile), or a trailing-dot name has NO suffix)
 * 4. missing suffix -> `missingSuffixFallback` (see below)
 * 5. lowercase-suffix membership in the supplied set
 *
 * `missingSuffixFallback` exists because the two server pipelines differ here,
 * and reusing one shared predicate across both was review finding PRR-002:
 *   * Documents (`documents.py` ~L2637-2643) maps a name that sanitizes to
 *     empty to `unnamed_file.txt` AND appends `.txt` to any extensionless
 *     name, so it accepts `README` iff `.txt` is allowed. That is the DEFAULT
 *     here (K-16), and it is what the Composer and Documents surfaces use.
 *   * Draft Room (`draft_input_storage.py` ~L148-153) maps empty to
 *     `unnamed_file` — with NO extension — and applies no extensionless
 *     fallback at all, so `Path("README").suffix` is `""` and is never a
 *     member. Pass `null` on that surface, otherwise the client accepts
 *     `README`/`Makefile`/`.gitignore`, transfers the whole body, and the
 *     server answers 415 unsupported_input.
 */
export function serverAcceptsFilename(
  name: string,
  allowedExtensions: string[],
  { missingSuffixFallback = ".txt" }: { missingSuffixFallback?: string | null } = {}
): boolean {
  const allowed = new Set(allowedExtensions);
  // POSIX os.path.basename: keep only the segment after the last "/".
  const base = name.split("/").pop() ?? name;
  // secure_filename: spaces -> underscores, then ASCII-only, then keep
  // [A-Za-z0-9._-]. The char-code filter performs the last two steps in one
  // pass without control-character regexes.
  const spaced = base.replace(/ /g, "_");
  let sanitized = "";
  for (let i = 0; i < spaced.length; i++) {
    const code = spaced.charCodeAt(i);
    const ch = spaced[i];
    if (code <= 127 && /[A-Za-z0-9._-]/.test(ch)) {
      sanitized += ch;
    }
  }
  const lastDot = sanitized.lastIndexOf(".");
  let suffix = "";
  if (lastDot > 0 && lastDot < sanitized.length - 1) {
    suffix = sanitized.slice(lastDot).toLowerCase();
  }
  if (suffix === "") {
    // Documents pipeline: secure_filename('') -> '' -> 'unnamed_file.txt'
    // -> suffix '.txt'. Draft Room passes null instead and is then correctly
    // rejected here, because '' is never a member of the allowlist.
    if (missingSuffixFallback === null) return false;
    suffix = missingSuffixFallback;
  }
  return allowed.has(suffix);
}

/**
 * Build the react-dropzone `accept` option from the normalized server
 * allowlist (issue #768 AC9 / plan Design D).
 *
 * The single valid MIME key `application/octet-stream` keeps the option free
 * of pickerOptionsFromAccept warnings (it validates keys), and the flattened
 * attribute ("application/octet-stream,.pdf,...") still hands the extension
 * list to the native file picker. The attribute is advisory only: the two
 * bounded divergence channels it creates (octet-stream-typed files passing
 * the attribute; extensionless names failing it) are resolved by
 * `serverAcceptsFilename` on both drop paths in UploadDropzone.
 */
export function uploadAcceptOption(
  allowedExtensions: string[]
): Record<string, string[]> | undefined {
  const normalized = Array.from(new Set(allowedExtensions.map(normalizeAllowedExtension))).sort();
  if (normalized.length === 0) return undefined;
  return { "application/octet-stream": normalized };
}

/**
 * Raster image extensions, mirroring backend RASTER_IMAGE_EXTENSIONS
 * (backend/app/services/document_artifacts.py). Exported for tests and for
 * any future surface that needs the same exclusion.
 */
export const RASTER_IMAGE_EXTENSIONS: readonly string[] = [
  ".png",
  ".jpg",
  ".jpeg",
  ".gif",
  ".bmp",
  ".tif",
  ".tiff",
  ".webp",
];

/**
 * The Draft Room surface allowlist: the server allowlist minus raster
 * images, mirroring backend DRAFT_ROOM_ALLOWED_EXTENSIONS
 * (backend/app/api/routes/draft_room.py — Draft Room is a parse-only text
 * seam, issue #460, so the vault image allowlist does not apply there).
 * Entries are normalized first so the subtraction sees the same
 * lowercase/dot-prefixed form serverAcceptsFilename membership uses.
 */
export function draftRoomAllowedExtensions(serverAllowedExtensions: string[]): string[] {
  const raster = new Set(RASTER_IMAGE_EXTENSIONS);
  return serverAllowedExtensions
    .map(normalizeAllowedExtension)
    .filter((ext) => !raster.has(ext));
}
