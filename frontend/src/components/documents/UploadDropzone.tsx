import { useCallback } from "react";
import { useDropzone, type FileRejection } from "react-dropzone";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Upload, Info } from "lucide-react";
import { useSettingsStore } from "@/stores/useSettingsStore";
import {
  MAX_UPLOAD_FILE_SIZE_MB,
  formatUploadSizeLimit,
  normalizeAllowedExtension,
  serverAcceptsFilename,
  uploadAcceptOption,
} from "@/lib/uploadLimits";

interface UploadDropzoneProps {
  hasSelectedVault: boolean;
  canWriteActiveVault: boolean;
  hasActiveVaultId: boolean;
  onFiles: (files: File[]) => void;
  onRejected: (names: string[]) => void;
}

export function UploadDropzone({
  hasSelectedVault,
  canWriteActiveVault,
  hasActiveVaultId,
  onFiles,
  onRejected,
}: UploadDropzoneProps) {
  // Effective upload limit: server-configured max_file_size_mb once settings
  // have loaded (the settings store is the only fetch path), else the client
  // fallback. Drives both react-dropzone's maxSize and the label.
  const maxFileSizeMb =
    useSettingsStore((s) => s.settings?.max_file_size_mb) ?? MAX_UPLOAD_FILE_SIZE_MB;
  const maxFileSizeBytes = maxFileSizeMb * 1024 * 1024;
  // Server allowlist: undefined = not loaded (server validates; today's
  // behavior); [] = loaded but empty (server rejects every suffix).
  const rawAllowedExtensions = useSettingsStore((s) => s.settings?.allowed_extensions);
  const allowedExtensionsLoaded = rawAllowedExtensions !== undefined;
  const allowedExtensions = Array.isArray(rawAllowedExtensions)
    ? rawAllowedExtensions.map(normalizeAllowedExtension)
    : null;

  // The accept option is the ADVISORY picker/drop filter (issue #768 AC9,
  // plan Design D): one valid MIME key (no pickerOptionsFromAccept warnings)
  // carrying the extension list to the native picker. Acceptance TRUTH is
  // serverAcceptsFilename, applied on both drop paths below — the two bounded
  // attribute/predicate divergence channels are resolved there.
  const accept = allowedExtensions ? uploadAcceptOption(allowedExtensions) : undefined;

  const onDrop = useCallback(
    (acceptedFiles: File[], rejections: FileRejection[]) => {
      // Channel (a): attr-accepted files re-checked by the predicate (catches
      // octet-stream-typed files the wildcard key admitted).
      const predicateAccepted = allowedExtensionsLoaded
        ? acceptedFiles.filter((file) => serverAcceptsFilename(file.name, allowedExtensions ?? []))
        : acceptedFiles;
      const predicateRejected = allowedExtensionsLoaded
        ? acceptedFiles.filter(
            (file) => !serverAcceptsFilename(file.name, allowedExtensions ?? [])
          )
        : [];
      // Channel (b), single recovery owner (plan-critic Round 4): recover
      // TYPE-ONLY attribute rejections the server would accept (e.g.
      // extensionless names that become .txt). Size-only and mixed
      // type+size rejections stay rejected — the size contract is not
      // recoverable.
      const recovered: File[] = [];
      const stillRejected: FileRejection[] = [];
      for (const rejection of rejections) {
        const typeOnly = rejection.errors.every((e) => e.code === "file-invalid-type");
        if (typeOnly && allowedExtensionsLoaded && serverAcceptsFilename(rejection.file.name, allowedExtensions ?? [])) {
          recovered.push(rejection.file);
        } else {
          stillRejected.push(rejection);
        }
      }
      const queueable = [...predicateAccepted, ...recovered];
      if (queueable.length > 0) {
        onFiles(queueable);
      }
      if (predicateRejected.length > 0 || stillRejected.length > 0) {
        const names = [
          ...predicateRejected.map((file) => `${file.name} (file type not allowed)`),
          ...stillRejected.map(
            (r) => `${r.file.name} (${r.errors.map((e) => e.message).join(", ")})`
          ),
        ];
        onRejected(names);
      }
    },
    [allowedExtensions, allowedExtensionsLoaded, onFiles, onRejected]
  );

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    maxSize: maxFileSizeBytes,
    disabled: !hasSelectedVault || !canWriteActiveVault,
    accept,
  });

  const disabled = !hasSelectedVault || !canWriteActiveVault;
  // Advertised types: the server allowlist once loaded; the historical
  // hardcoded copy is only the pre-load fallback (issue #768 AC8).
  const advertisedTypes = allowedExtensions
    ? Array.from(new Set(allowedExtensions))
        .sort()
        .map((ext) => ext.replace(/^\./, "").toUpperCase())
        .join(", ")
    : "PDF, DOCX, TXT, MD";

  return (
    <Card
      {...getRootProps()}
      className={`border-2 border-dashed cursor-pointer transition-colors ${
        isDragActive ? "border-primary bg-primary/5" : "border-border"
      } ${disabled ? "cursor-not-allowed" : ""}`}
    >
      {/* Visually-hidden-equivalent label: the dropzone root is the click
          surface, but the rendered <input type="file"> still needs an
          accessible name (ENH-005 axe `label` rule). */}
      <input {...getInputProps()} aria-label="Upload files" />
      {!hasActiveVaultId && (
        <div className="text-muted-foreground text-sm p-4 text-center">
          Select a vault to upload documents.
        </div>
      )}
      <CardContent className="py-8">
        <div className="flex flex-col items-center justify-center text-center">
          <Badge variant="secondary" className="mb-3 gap-1.5 text-xs font-medium">
            <Info className="h-3 w-3" aria-hidden="true" />
            Max {formatUploadSizeLimit(maxFileSizeMb)}
          </Badge>
          {/* Only the icon affordance dims in the disabled state — the
              instruction text keeps full contrast (issue #768 AC10; the
              ancestor opacity dropped it below WCAG AA). */}
          <Upload
            className={`w-12 h-12 text-muted-foreground mb-4 ${disabled ? "opacity-60" : ""}`}
            aria-hidden="true"
          />
          <p className="text-lg font-medium">
            {disabled
              ? "Select a writable vault to upload"
              : isDragActive
                ? "Drop files here..."
                : "Drag & drop files here, or click to select"}
          </p>
          <p className="text-sm text-muted-foreground mt-1">
            Supports {advertisedTypes} files (max {formatUploadSizeLimit(maxFileSizeMb)} each). Uploads continue in background.
          </p>
        </div>
      </CardContent>
    </Card>
  );
}
