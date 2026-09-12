import { useCallback } from "react";
import { useDropzone, type FileRejection } from "react-dropzone";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Upload, Info } from "lucide-react";
import { useSettingsStore } from "@/stores/useSettingsStore";
import { MAX_UPLOAD_FILE_SIZE_MB, formatUploadSizeLimit } from "@/lib/uploadLimits";

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

  const onDrop = useCallback(
    (acceptedFiles: File[]) => {
      if (acceptedFiles.length === 0) return;
      onFiles(acceptedFiles);
    },
    [onFiles]
  );

  const onDropRejected = useCallback(
    (rejected: FileRejection[]) => {
      const rejectedNames = rejected.map(
        (r) => `${r.file.name} (${r.errors.map((e) => e.message).join(", ")})`
      );
      onRejected(rejectedNames);
    },
    [onRejected]
  );

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    onDropRejected,
    maxSize: maxFileSizeBytes,
    disabled: !hasSelectedVault || !canWriteActiveVault,
  });

  return (
    <Card
      {...getRootProps()}
      className={`border-2 border-dashed cursor-pointer transition-colors ${
        isDragActive ? "border-primary bg-primary/5" : "border-border"
      } ${!hasSelectedVault || !canWriteActiveVault ? "opacity-60 cursor-not-allowed" : ""}`}
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
          <Upload className="w-12 h-12 text-muted-foreground mb-4" />
          <p className="text-lg font-medium">
            {!hasSelectedVault || !canWriteActiveVault
              ? "Select a writable vault to upload"
              : isDragActive
                ? "Drop files here..."
                : "Drag & drop files here, or click to select"}
          </p>
          <p className="text-sm text-muted-foreground mt-1">
            Supports PDF, DOCX, TXT, MD files (max {formatUploadSizeLimit(maxFileSizeMb)} each). Uploads continue in background.
          </p>
        </div>
      </CardContent>
    </Card>
  );
}
