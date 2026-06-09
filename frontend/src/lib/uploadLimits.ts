export const MAX_UPLOAD_FILE_SIZE_MB = 100;
export const MAX_UPLOAD_FILE_SIZE_BYTES = MAX_UPLOAD_FILE_SIZE_MB * 1024 * 1024;

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
  maxFileSizeBytes = MAX_UPLOAD_FILE_SIZE_BYTES
): boolean {
  return file.size > maxFileSizeBytes;
}

export function normalizeUploadErrorMessage(error: unknown): string {
  const message = error instanceof Error ? error.message : "Upload failed";
  const status = typeof error === "object" && error !== null
    ? (error as { status?: number }).status
    : undefined;
  if (status === 413 || /request entity too large|payload too large|file too large/i.test(message)) {
    return `File too large. Max size: ${formatUploadSizeLimit()}.`;
  }
  return message;
}
