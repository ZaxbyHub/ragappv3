import { useCallback, useEffect, useId, useRef, useState } from "react";
import type { ChangeEvent } from "react";
import { useDropzone } from "react-dropzone";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Upload, Loader2, CheckCircle2, XCircle, Clock, RotateCcw, X } from "lucide-react";
import { Button, buttonVariants } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { cn } from "@/lib/utils";
import { ADD_SOURCE_FILES_CTA, INPUT_AUTHORITY_LABELS, INPUT_ROLE_LABELS } from "./labels";
import {
  DRAFT_INPUT_AUTHORITIES,
  DRAFT_INPUT_ROLES,
  draftRoomKeys,
  getDraftInputContent,
  parseDraftRoomError,
  uploadDraftInput,
  type DraftInputAuthority,
  type DraftInputParseStatus,
  type DraftInputRole,
  type DraftInputUploadResponse,
  type DraftRoomErrorInfo,
} from "@/lib/api/draftRoom";

export interface DraftSourceUploadProps {
  draftId: number;
  disabled?: boolean;
  disabledReason?: string;
  maxInputs: number;
  currentInputCount: number;
  onUploaded?(response: DraftInputUploadResponse): void;
}

type UploadItemStatus = "queued" | "uploading" | "parsing" | "ready" | "failed";

interface UploadItem {
  id: string;
  file: File;
  role: DraftInputRole;
  authority: DraftInputAuthority;
  asOfDate: string;
  status: UploadItemStatus;
  progress: number;
  inputId: number | null;
  errorDetail: string | null;
  /** Retry re-runs the upload call; only legal before an input row exists on the server. */
  canRetry: boolean;
}

let uploadItemSeq = 0;
function nextUploadItemId(): string {
  uploadItemSeq += 1;
  return `draft-source-${uploadItemSeq}`;
}

function explainUploadError(info: DraftRoomErrorInfo): { message: string; canRetry: boolean } {
  switch (info.code) {
    case "duplicate_input": {
      const existingId =
        typeof info.context.existing_input_id === "number" ? info.context.existing_input_id : null;
      return {
        message:
          existingId != null
            ? `${info.detail} This matches input #${existingId} already in this project.`
            : info.detail,
        canRetry: false,
      };
    }
    case "input_too_large":
    case "unsupported_input":
    case "limit_exceeded":
      return { message: info.detail, canRetry: false };
    default:
      return { message: info.detail, canRetry: true };
  }
}

const PARSE_FAILED_GUIDANCE =
  "Parsing failed. Delete this source in the list below and upload the file again.";

function StatusIndicator({ status }: { status: UploadItemStatus }) {
  switch (status) {
    case "queued":
      return (
        <span className="flex items-center gap-1 text-xs text-muted-foreground">
          <Clock className="h-3.5 w-3.5" aria-hidden="true" />
          Queued
        </span>
      );
    case "uploading":
      return (
        <span className="flex items-center gap-1 text-xs text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
          Uploading
        </span>
      );
    case "parsing":
      return (
        <span className="flex items-center gap-1 text-xs text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
          Parsing
        </span>
      );
    case "ready":
      return (
        <span className="flex items-center gap-1 text-xs text-success">
          <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
          Parsed
        </span>
      );
    case "failed":
      return (
        <span className="flex items-center gap-1 text-xs text-destructive">
          <XCircle className="h-3.5 w-3.5" aria-hidden="true" />
          Failed
        </span>
      );
    default:
      return null;
  }
}

/**
 * Invisible per-row watcher: once an input row exists, polls its parse status
 * until it leaves pending/parsing, then reports the terminal status exactly once.
 */
function ParsingWatcher({
  draftId,
  inputId,
  onResolved,
}: {
  draftId: number;
  inputId: number;
  onResolved: (status: DraftInputParseStatus) => void;
}) {
  const resolvedRef = useRef(false);
  const { data } = useQuery({
    queryKey: ["draft-room-source-upload-watch", draftId, inputId],
    queryFn: () => getDraftInputContent(draftId, inputId),
    refetchInterval: (query) => {
      const status = query.state.data?.parse_status;
      return status && status !== "pending" && status !== "parsing" ? false : 2000;
    },
  });

  useEffect(() => {
    if (!data || resolvedRef.current) return;
    if (data.parse_status !== "pending" && data.parse_status !== "parsing") {
      resolvedRef.current = true;
      onResolved(data.parse_status);
    }
  }, [data, onResolved]);

  return null;
}

export function DraftSourceUpload({
  draftId,
  disabled = false,
  disabledReason,
  maxInputs,
  currentInputCount,
  onUploaded,
}: DraftSourceUploadProps) {
  const idPrefix = useId();
  const queryClient = useQueryClient();
  const [role, setRole] = useState<DraftInputRole>("reference");
  const [authority, setAuthority] = useState<DraftInputAuthority>("unknown");
  const [asOfDate, setAsOfDate] = useState("");
  const [items, setItems] = useState<UploadItem[]>([]);

  const capReached = currentInputCount >= maxInputs;
  const isUploadDisabled = disabled || capReached;

  const invalidateDraft = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: draftRoomKeys.detail(draftId) });
    queryClient.invalidateQueries({ queryKey: draftRoomKeys.inputs(draftId) });
  }, [draftId, queryClient]);

  const startUpload = useCallback(
    async (item: UploadItem) => {
      setItems((prev) =>
        prev.map((it) => (it.id === item.id ? { ...it, status: "uploading", progress: 0 } : it))
      );
      try {
        const response = await uploadDraftInput(
          draftId,
          {
            file: item.file,
            role: item.role,
            authority: item.authority,
            as_of_date: item.asOfDate || undefined,
          },
          (percent) => {
            setItems((prev) => prev.map((it) => (it.id === item.id ? { ...it, progress: percent } : it)));
          }
        );
        onUploaded?.(response);
        invalidateDraft();
        const parseStatus = response.input.parse_status;
        const nextStatus: UploadItemStatus =
          parseStatus === "ready" ? "ready" : parseStatus === "failed" || parseStatus === "cancelled" ? "failed" : "parsing";
        setItems((prev) =>
          prev.map((it) =>
            it.id === item.id
              ? {
                  ...it,
                  inputId: response.input.id,
                  status: nextStatus,
                  progress: 100,
                  errorDetail: nextStatus === "failed" ? PARSE_FAILED_GUIDANCE : null,
                  canRetry: false,
                }
              : it
          )
        );
      } catch (err) {
        const info = parseDraftRoomError(err);
        const explanation = explainUploadError(info);
        setItems((prev) =>
          prev.map((it) =>
            it.id === item.id
              ? {
                  ...it,
                  status: "failed",
                  progress: 0,
                  errorDetail: explanation.message,
                  canRetry: explanation.canRetry,
                }
              : it
          )
        );
      }
    },
    [draftId, invalidateDraft, onUploaded]
  );

  useEffect(() => {
    items
      .filter((it) => it.status === "queued")
      .forEach((it) => {
        void startUpload(it);
      });
  }, [items, startUpload]);

  const handleParseResolved = useCallback(
    (itemId: string, status: DraftInputParseStatus) => {
      setItems((prev) =>
        prev.map((it) => {
          if (it.id !== itemId || it.status !== "parsing") return it;
          const failed = status === "failed" || status === "cancelled";
          return {
            ...it,
            status: failed ? "failed" : "ready",
            errorDetail: failed ? PARSE_FAILED_GUIDANCE : null,
            canRetry: false,
          };
        })
      );
      invalidateDraft();
    },
    [invalidateDraft]
  );

  const addFiles = useCallback(
    (files: File[]) => {
      if (files.length === 0) return;
      setItems((prev) => [
        ...prev,
        ...files.map(
          (file): UploadItem => ({
            id: nextUploadItemId(),
            file,
            role,
            authority,
            asOfDate,
            status: "queued",
            progress: 0,
            inputId: null,
            errorDetail: null,
            canRetry: false,
          })
        ),
      ]);
    },
    [role, authority, asOfDate]
  );

  const handleFileInputChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      const files = event.target.files ? Array.from(event.target.files) : [];
      addFiles(files);
      event.target.value = "";
    },
    [addFiles]
  );

  const retryItem = useCallback((itemId: string) => {
    setItems((prev) =>
      prev.map((it) =>
        it.id === itemId ? { ...it, status: "queued", progress: 0, errorDetail: null } : it
      )
    );
  }, []);

  const removeItem = useCallback((itemId: string) => {
    setItems((prev) => prev.filter((it) => it.id !== itemId));
  }, []);

  const { getRootProps, isDragActive } = useDropzone({
    onDrop: addFiles,
    noClick: true,
    noKeyboard: true,
    disabled: isUploadDisabled,
  });

  const fileInputId = `${idPrefix}-file-input`;
  const capMessage = `This project already holds the maximum of ${maxInputs} source file${
    maxInputs === 1 ? "" : "s"
  }.`;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-4">
        <div className="space-y-1">
          <Label htmlFor={`${idPrefix}-role`}>Role</Label>
          <Select value={role} onValueChange={(value) => setRole(value as DraftInputRole)} disabled={isUploadDisabled}>
            <SelectTrigger id={`${idPrefix}-role`} className="w-44" aria-label="Role">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {DRAFT_INPUT_ROLES.map((value) => (
                <SelectItem key={value} value={value}>
                  {INPUT_ROLE_LABELS[value]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1">
          <Label htmlFor={`${idPrefix}-authority`}>Authority</Label>
          <Select
            value={authority}
            onValueChange={(value) => setAuthority(value as DraftInputAuthority)}
            disabled={isUploadDisabled}
          >
            <SelectTrigger id={`${idPrefix}-authority`} className="w-44" aria-label="Authority">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {DRAFT_INPUT_AUTHORITIES.map((value) => (
                <SelectItem key={value} value={value}>
                  {INPUT_AUTHORITY_LABELS[value]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1">
          <Label htmlFor={`${idPrefix}-as-of`}>As-of date (optional)</Label>
          <Input
            id={`${idPrefix}-as-of`}
            type="date"
            value={asOfDate}
            onChange={(event) => setAsOfDate(event.target.value)}
            disabled={isUploadDisabled}
            className="w-44"
          />
        </div>
      </div>

      <div
        {...getRootProps()}
        className={cn(
          "rounded-md border-2 border-dashed p-6 text-center transition-colors",
          isDragActive ? "border-primary bg-primary/5" : "border-border",
          isUploadDisabled && "opacity-60"
        )}
      >
        <Upload className="mx-auto mb-2 h-8 w-8 text-muted-foreground" aria-hidden="true" />
        <p className="mb-3 text-sm text-muted-foreground">Drag and drop files here, or</p>
        <input
          id={fileInputId}
          type="file"
          multiple
          onChange={handleFileInputChange}
          disabled={isUploadDisabled}
          className="sr-only"
        />
        <Label
          htmlFor={fileInputId}
          className={cn(
            buttonVariants({ variant: "outline" }),
            "cursor-pointer",
            isUploadDisabled && "pointer-events-none opacity-50"
          )}
        >
          {ADD_SOURCE_FILES_CTA}
        </Label>
      </div>

      {disabled && disabledReason && (
        <p className="text-sm text-muted-foreground" role="status">
          {disabledReason}
        </p>
      )}
      {!disabled && capReached && (
        <p className="text-sm text-warning" role="status">
          {capMessage}
        </p>
      )}

      {items.length > 0 && (
        <ul className="space-y-2">
          {items.map((item) => (
            <li key={item.id} className="min-w-0 rounded-md border border-border p-3">
              <div className="flex min-w-0 items-center justify-between gap-2">
                <span className="min-w-0 flex-1 truncate text-sm font-medium">{item.file.name}</span>
                <StatusIndicator status={item.status} />
              </div>
              {item.status === "uploading" && (
                <Progress
                  value={item.progress}
                  aria-label={`Upload progress for ${item.file.name}`}
                  className="mt-2"
                />
              )}
              {item.status === "failed" && item.errorDetail && (
                <div className="mt-2 space-y-2">
                  <p className="text-sm text-destructive">{item.errorDetail}</p>
                  <div className="flex gap-2">
                    {item.canRetry && (
                      <Button type="button" size="sm" variant="outline" onClick={() => retryItem(item.id)}>
                        <RotateCcw className="mr-1 h-3.5 w-3.5" aria-hidden="true" />
                        Retry
                      </Button>
                    )}
                    <Button type="button" size="sm" variant="ghost" onClick={() => removeItem(item.id)}>
                      <X className="mr-1 h-3.5 w-3.5" aria-hidden="true" />
                      Remove
                    </Button>
                  </div>
                </div>
              )}
              {item.status === "parsing" && item.inputId != null && (
                <ParsingWatcher
                  draftId={draftId}
                  inputId={item.inputId}
                  onResolved={(status) => handleParseResolved(item.id, status)}
                />
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
