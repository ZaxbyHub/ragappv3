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
 * documents.py's extension check) — step for step, INCLUDING the allowlist
 * shape: membership is tested against the RAW configured entries, exactly as
 * the server compares Path(file_name).suffix.lower() against
 * settings.allowed_extensions (PRR-004: the client previously normalized
 * entries the server does not, so an uppercase/dotless configured entry
 * accepted client-side and rejected server-side).
 *
 * 1. basename with POSIX semantics: split on "/" only (PRR-003: the server's
 *    os.path.basename on Linux treats backslash as an ordinary character;
 *    the previous both-separator split accepted backslash names the server
 *    rejects — moot for browser File.name, but the mirror must not diverge)
 * 2. spaces -> underscores, ASCII-only, keep [A-Za-z0-9._-] (secure_filename)
 * 3. final suffix with Python Path.suffix semantics: the last dot only
 *    when it is at index > 0 AND before the last character (a no-dot name, a
 *    leading-dot-only name (dotfile), or a trailing-dot name has NO suffix)
 * 4. missing suffix -> the server's .txt fallback (K-16: this INCLUDES a
 *    name that sanitizes to empty — the server renames it to
 *    unnamed_file.txt and accepts it iff .txt is allowed; the previous
 *    early false rejected server-accepted uploads)
 * 5. lowercase-suffix membership in the RAW configured set
 */
export function serverAcceptsFilename(name: string, allowedExtensions: string[]): boolean {
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
    // The server's missing-suffix fallback: secure_filename('') -> '' ->
    // 'unnamed_file.txt' -> suffix '.txt'.
    suffix = ".txt";
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
