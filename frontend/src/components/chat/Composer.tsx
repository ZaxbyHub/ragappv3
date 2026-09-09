// Composer — extracted from TranscriptPane for maintainability.
// Handles draft persistence, auto-grow, slash commands, IME guard, file attachments.

import { useRef, useEffect, useState, useCallback } from "react";
import { useDropzone } from "react-dropzone";
import {
  Send,
  Square,
  Slash,
  Database,
  FileText,
  GitCompare,
  Calendar,
  ListChecks,
  Paperclip,
  X,
  AlertCircle,
} from "lucide-react";
import { motion, AnimatePresence } from "framer-motion";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { cn } from "@/lib/utils";
import { useChatStore } from "@/stores/useChatStore";
import { useChatModeStore } from "@/stores/useChatModeStore";
import { useLlmHealthStore } from "@/stores/useLlmHealthStore";
import { useSettingsStore } from "@/stores/useSettingsStore";
import { useVaultStore } from "@/stores/useVaultStore";
import { useUploadStore } from "@/stores/useUploadStore";
import type { UploadFile } from "@/stores/useUploadStore";
import { useUploadMonitoring } from "@/hooks/useUploadMonitoring";
import { computeEffectiveChatMode } from "@/lib/chatMode";
import { MAX_INPUT_LENGTH } from "@/hooks/useSendMessage";
import { useEscapeToStop } from "@/hooks/useEscapeToStop";
import { toast } from "sonner";

// =============================================================================
// Types
// =============================================================================

interface SlashCommand {
  id: string;
  label: string;
  description: string;
  icon: React.ReactNode;
}

/**
 * State machine for an attachment chip from the moment the user drops it
 * onto the composer until it is fully indexed and searchable by RAG:
 *
 *   uploading → uploaded → indexing → indexed
 *                                  ↳ error (terminal, with message)
 *
 * The user can submit a query containing an attachment that is still in
 * the ``uploading``, ``uploaded``, or ``indexing`` states, but the UI shows
 * a warning that the file is not yet searchable. Failures surface the
 * backend error message and stay removable. Attachment state lives in the
 * shared upload store, so chips survive composer unmount/remount and their
 * indexing progress is driven by the shared batched status monitor.
 */
type AttachmentChipStatus =
  | "uploading"
  | "uploaded"
  | "indexing"
  | "indexed"
  | "error";

/**
 * `uploading` covers queued + in-flight byte transfers; `uploaded` means the
 * server accepted the bytes but no status snapshot has arrived yet; once the
 * monitor applies a non-terminal snapshot the chip shows `indexing`.
 */
function attachmentChipStatus(upload: UploadFile): AttachmentChipStatus {
  switch (upload.status) {
    case "pending":
    case "uploading":
      return "uploading";
    case "indexed":
      return "indexed";
    case "error":
    case "cancelled":
      return "error";
    default:
      // processing / indexing: bytes accepted.
      return upload.statusSeen ? "indexing" : "uploaded";
  }
}

interface ComposerProps {
  onSend: () => void;
  onStop: () => void;
  isStreaming: boolean;
  className?: string;
  inputRef?: React.RefObject<HTMLTextAreaElement | null>;
}

// =============================================================================
// Constants
// =============================================================================

const SLASH_COMMANDS: SlashCommand[] = [
  { id: "summarize", label: "/summarize", description: "Summarize this document", icon: <FileText className="h-4 w-4" /> },
  { id: "compare",   label: "/compare",   description: "Compare these sources",   icon: <GitCompare className="h-4 w-4" /> },
  { id: "timeline",  label: "/timeline",  description: "Create a timeline",        icon: <Calendar className="h-4 w-4" /> },
  { id: "actions",   label: "/actions",   description: "List action items",        icon: <ListChecks className="h-4 w-4" /> },
];

const DRAFT_PREFIX = "ragapp_chat_draft_";
const getDraftKey = (sessionId: string | null) => `${DRAFT_PREFIX}${sessionId ?? "new"}`;

// =============================================================================
// Composer
// =============================================================================

export function Composer({ onSend, onStop, isStreaming, className, inputRef }: ComposerProps) {
  const internalRef = useRef<HTMLTextAreaElement>(null);
  const textareaRef = (inputRef ?? internalRef) as React.RefObject<HTMLTextAreaElement>;

  const { input, setInput, inputError, activeChatId } = useChatStore();
  const storedChatMode = useChatModeStore((s) => s.chatMode);
  const setStoredChatMode = useChatModeStore((s) => s.setChatMode);
  const temperature = useChatModeStore((s) => s.temperature);
  const setTemperature = useChatModeStore((s) => s.setTemperature);
  const retrievalMode = useChatModeStore((s) => s.retrievalMode);
  const setRetrievalMode = useChatModeStore((s) => s.setRetrievalMode);
  const citationMode = useChatModeStore((s) => s.citationMode);
  const setCitationMode = useChatModeStore((s) => s.setCitationMode);
  const metadataFilterDateFrom = useChatModeStore((s) => s.metadataFilterDateFrom);
  const setMetadataFilterDateFrom = useChatModeStore((s) => s.setMetadataFilterDateFrom);
  const metadataFilterDateTo = useChatModeStore((s) => s.metadataFilterDateTo);
  const setMetadataFilterDateTo = useChatModeStore((s) => s.setMetadataFilterDateTo);
  const metadataFilterTags = useChatModeStore((s) => s.metadataFilterTags);
  const setMetadataFilterTags = useChatModeStore((s) => s.setMetadataFilterTags);
  const metadataFilterAuthor = useChatModeStore((s) => s.metadataFilterAuthor);
  const setMetadataFilterAuthor = useChatModeStore((s) => s.setMetadataFilterAuthor);
  const defaultChatMode = useSettingsStore((s) => s.formData.default_chat_mode);
  const thinkingHealthy = useLlmHealthStore((s) => s.thinking);
  const instantHealthy = useLlmHealthStore((s) => s.instant);
  const refreshLlmHealth = useLlmHealthStore((s) => s.refresh);
  // Reflects the mode that will actually be sent, including health fallback,
  // so the toggle highlight cannot disagree with the request payload.
  const effectiveChatMode = computeEffectiveChatMode({
    stored: storedChatMode,
    defaultMode: defaultChatMode,
    thinkingHealthy,
    instantHealthy,
  });
  const desiredChatMode: "instant" | "thinking" =
    storedChatMode ?? defaultChatMode ?? "thinking";
  const isFallenBack = effectiveChatMode !== desiredChatMode;

  // Poll backend LLM health on mount and every 30s so the Instant toggle
  // can disable when LM Studio is unreachable.
  useEffect(() => {
    refreshLlmHealth();
    const handle = setInterval(refreshLlmHealth, 30000);
    return () => clearInterval(handle);
  }, [refreshLlmHealth]);

  const { getActiveVault } = useVaultStore();
  const activeVault = getActiveVault();
  const activeVaultId = useVaultStore((s) => s.activeVaultId);

  // Slash command menu
  const [showSlashMenu, setShowSlashMenu] = useState(false);
  const [selectedCmd, setSelectedCmd] = useState(0);
  const [slashQuery, setSlashQuery] = useState("");
  const closeSlashMenu = useCallback(() => {
    setShowSlashMenu(false);
    setSlashQuery("");
    setSelectedCmd(0);
  }, []);

  // File attachments — shared upload store. Transfers run in the store's
  // bounded pool and indexing is monitored by the shared batched status
  // poller, so chips (and their progress) survive unmount/remount.
  const uploads = useUploadStore((s) => s.uploads);
  const chatAttachmentIds = useUploadStore((s) => s.chatAttachmentIds);
  const addUploads = useUploadStore((s) => s.addUploads);
  const attachToChat = useUploadStore((s) => s.attachToChat);
  const detachFromChat = useUploadStore((s) => s.detachFromChat);
  const removeUpload = useUploadStore((s) => s.removeUpload);
  useUploadMonitoring();

  const attachments: UploadFile[] = chatAttachmentIds
    .map((id) => uploads.find((u) => u.id === id))
    .filter((u): u is UploadFile => u !== undefined);
  const attachmentChips = attachments.map((upload) => ({
    upload,
    status: attachmentChipStatus(upload),
  }));

  // Draft — restore on session change, write on input changes
  const lastLoadedRef = useRef<string | null>(null);
  useEffect(() => {
    if (typeof window === "undefined") return;
    const key = activeChatId ?? "new";
    if (lastLoadedRef.current === key) return;
    lastLoadedRef.current = key;
    try {
      setInput(localStorage.getItem(getDraftKey(activeChatId)) ?? "");
    } catch { /* private mode */ }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeChatId]);

  const persistDraft = useCallback((value: string) => {
    if (typeof window === "undefined") return;
    try {
      const k = getDraftKey(activeChatId);
      if (value) localStorage.setItem(k, value);
      else localStorage.removeItem(k);
    } catch { /* ignore */ }
  }, [activeChatId]);

  // Esc-to-stop: while a response is streaming, Escape stops generation.
  useEscapeToStop(isStreaming, onStop);

  // Auto-grow
  const adjustHeight = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.max(44, Math.min(el.scrollHeight, 200))}px`;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => { adjustHeight(); }, [input, adjustHeight]);

  // Filtered slash commands
  const filteredCmds = SLASH_COMMANDS.filter((c) =>
    c.label.toLowerCase().includes(slashQuery.toLowerCase())
  );

  const insertCommand = useCallback((cmd: SlashCommand) => {
    const lines = input.split("\n");
    lines[lines.length - 1] = cmd.label + " ";
    const next = lines.join("\n");
    setInput(next);
    persistDraft(next);
    closeSlashMenu();
    textareaRef.current?.focus();
  }, [input, setInput, persistDraft, textareaRef, closeSlashMenu]);

  // Input change
  const handleChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const value = e.target.value;
    setInput(value);
    persistDraft(value);

    const lines = value.split("\n");
    const last = lines[lines.length - 1];
    if (last.startsWith("/") && !last.includes(" ")) {
      setShowSlashMenu(true);
      setSlashQuery(last.slice(1));
      setSelectedCmd(0);
    } else {
      closeSlashMenu();
    }
  };

  // Keyboard handler with IME guard
  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (showSlashMenu) {
      if (e.key === "ArrowDown") { e.preventDefault(); setSelectedCmd((p) => Math.min(p + 1, filteredCmds.length - 1)); return; }
      if (e.key === "ArrowUp")   { e.preventDefault(); setSelectedCmd((p) => Math.max(p - 1, 0)); return; }
      if (e.key === "Enter")     { e.preventDefault(); if (filteredCmds[selectedCmd]) insertCommand(filteredCmds[selectedCmd]); return; }
      if (e.key === "Escape")    { e.preventDefault(); setShowSlashMenu(false); return; }
    }
    // IME guard: nativeEvent.isComposing is true while CJK composition is in progress
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      handleSubmit();
    }
  };

  const handleSubmit = () => {
    if (!input.trim() || isStreaming || input.length > MAX_INPUT_LENGTH) return;
    // Hard-block sending while uploads are still transferring — there is
    // no file id yet to reference. Indexing-in-progress is a soft warning
    // only (the file may still be partially searchable, and we don't want
    // to block the user indefinitely if indexing is slow).
    if (attachmentChips.some((c) => c.status === "uploading")) {
      toast.error("Please wait for file uploads to complete.");
      return;
    }
    const indexingChips = attachmentChips.filter(
      (c) => c.status === "indexing" || c.status === "uploaded"
    );
    if (indexingChips.length > 0) {
      const indexingIds = new Set(indexingChips.map((c) => c.upload.id));
      toast.warning("Some files are still indexing", {
        description: `${indexingChips.map((c) => c.upload.file.name).join(", ")} may not be searchable yet.`,
        action: {
          label: "Send without these files",
          onClick: () => {
            indexingIds.forEach((id) => detachFromChat(id));
            persistDraft("");
            closeSlashMenu();
            onSend();
          },
        },
      });
      return;
    }
    persistDraft("");
    closeSlashMenu();
    onSend();
    // Keep attachments in the tray after send only if they failed —
    // fully indexed files have been used and empty-state chips add noise.
    attachmentChips
      .filter((c) => c.status !== "error")
      .forEach((c) => detachFromChat(c.upload.id));
  };

  // =============================================================================
  // File upload
  // =============================================================================

  // Paste/drop enqueue through the shared upload store's bounded transfer
  // pool; the returned ids register the files as chat attachments so the
  // chips live in the store (surviving navigation) instead of this mount.
  const enqueueFiles = useCallback(
    (files: File[]) => {
      if (files.length === 0) return;
      const queuedIds = addUploads(files, activeVaultId ?? undefined);
      queuedIds.forEach((id) => attachToChat(id));
    },
    [activeVaultId, addUploads, attachToChat]
  );

  const { getRootProps, getInputProps, isDragActive, open: openFilePicker } = useDropzone({
    noClick: true,
    noKeyboard: true,
    onDrop: enqueueFiles,
  });

  // Handle paste with files
  const handlePaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const files = Array.from(e.clipboardData.files);
    if (files.length > 0) {
      e.preventDefault();
      enqueueFiles(files);
    }
  };

  const removeAttachment = (id: string) => {
    removeUpload(id);
    detachFromChat(id);
  };

  const hasUploading = attachmentChips.some((c) => c.status === "uploading");
  const hasIndexing = attachmentChips.some(
    (c) => c.status === "indexing" || c.status === "uploaded"
  );

  // =============================================================================
  // Render
  // =============================================================================

  return (
    <TooltipProvider>
      <div className={cn("relative", className)} {...getRootProps()}>
        {/* Drag overlay */}
        {isDragActive && (
          <div className="absolute inset-0 z-20 flex items-center justify-center rounded-xl border-2 border-dashed border-primary bg-primary/5">
            <p className="text-sm font-medium text-primary">Drop files to upload</p>
          </div>
        )}

        {/* Vault context pill */}
        {activeVault && (
          <div className="mb-2 flex items-center gap-2">
            <Badge variant="secondary" className="gap-1.5 text-xs font-normal" aria-label={`Active vault: ${activeVault.name}`}>
              <Database className="h-3 w-3" aria-hidden />
              {activeVault.name}
            </Badge>
          </div>
        )}

        <span id="composer-help" className="sr-only">
          Press Enter to send, Shift+Enter for a new line, slash for commands, or paste/drop files to upload.
        </span>

        {/* Attachment tray */}
        {attachmentChips.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-2" data-testid="attachment-tray">
            {attachmentChips.map(({ upload: att, status }) => {
              const statusText = (() => {
                switch (status) {
                  case "uploading":
                    return `Uploading ${att.uploadProgress}%`;
                  case "uploaded":
                    return "Uploaded · waiting for indexer";
                  case "indexing":
                    return "Indexing for search…";
                  case "indexed":
                    return att.chunkCount
                      ? `Indexed · ${att.chunkCount} chunks`
                      : "Ready · indexed";
                  case "error":
                    return att.error ?? (att.status === "cancelled" ? "Cancelled" : "Failed");
                }
              })();
              return (
                <div
                  key={att.id}
                  data-testid={`attachment-${status}`}
                  className={cn(
                    "flex items-center gap-2 rounded-sm border px-2 py-1.5 text-xs",
                    status === "error" && "border-destructive/50 bg-destructive/5",
                    status === "indexed" && "border-success/50 bg-success/5",
                    status === "uploaded" && "border-amber-500/40 bg-amber-500/5",
                    status === "indexing" && "border-amber-500/40 bg-amber-500/5",
                    status === "uploading" && "border-border bg-muted/50"
                  )}
                  aria-label={`Attachment ${att.file.name}: ${statusText}`}
                >
                  {status === "error" ? (
                    <AlertCircle className="h-3.5 w-3.5 text-destructive shrink-0" />
                  ) : (
                    <FileText className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
                  )}
                  <span className="max-w-[120px] truncate" title={att.file.name}>
                    {att.file.name}
                  </span>
                  {status === "uploading" && (
                    <Progress value={att.uploadProgress} className="h-1 w-16" />
                  )}
                  <span
                    className={cn(
                      "text-[10px]",
                      status === "error" && "text-destructive",
                      status === "indexed" && "text-success",
                      (status === "uploaded" || status === "indexing") &&
                        "text-amber-700 dark:text-amber-300",
                      status === "uploading" && "text-muted-foreground"
                    )}
                  >
                    {statusText}
                  </span>
                  <button
                    onClick={() => removeAttachment(att.id)}
                    className="ml-1 text-muted-foreground hover:text-foreground"
                    aria-label={`Remove ${att.file.name}`}
                  >
                    <X className="h-3 w-3" />
                  </button>
                </div>
              );
            })}
            {hasIndexing && (
              <div
                role="status"
                className="text-[11px] text-amber-700 dark:text-amber-300 self-center"
                aria-live="polite"
              >
                Some attachments are still indexing — they may not be searchable yet.
              </div>
            )}
          </div>
        )}

        {/* Composer container */}
        <div
          className={cn(
            "relative rounded-xl border bg-card shadow-xs transition-all duration-150",
            "focus-within:border-primary/50 focus-within:shadow-md focus-within:shadow-primary/5",
            isStreaming ? "border-primary/30" : "border-input",
          )}
        >
          {/* Textarea — readOnly during streaming but still scrollable and accessible */}
          <Textarea
            ref={textareaRef as React.Ref<HTMLTextAreaElement>}
            value={input}
            onChange={handleChange}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            placeholder={isStreaming ? "Generating..." : "Message... (Enter to send · Shift+Enter for newline · / for commands)"}
            className={cn(
              "min-h-[44px] max-h-[200px] resize-none border-0 bg-transparent px-4 py-3",
              "text-sm placeholder:text-muted-foreground/60",
              "focus-visible:ring-0 focus-visible:ring-offset-0",
            )}
            readOnly={isStreaming}
            aria-label="Message input"
            aria-describedby={inputError ? "input-error composer-help" : "composer-help"}
            role="combobox"
            aria-expanded={showSlashMenu}
            aria-haspopup="listbox"
            aria-controls={showSlashMenu ? "slash-menu" : undefined}
            rows={1}
          />

          {/* Slash command menu */}
          <AnimatePresence>
            {showSlashMenu && (
              <motion.div
                id="slash-menu"
                role="listbox"
                aria-label="Slash commands"
                initial={{ opacity: 0, y: -6 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -6 }}
                transition={{ duration: 0.12 }}
                className="absolute bottom-full left-0 z-50 mb-2 w-72 overflow-hidden rounded-sm border border-border bg-popover shadow-lg"
              >
                <div className="max-h-64 overflow-y-auto py-1">
                  {filteredCmds.length === 0 ? (
                    <div className="px-3 py-4 text-center text-sm text-muted-foreground">No matching commands</div>
                  ) : (
                    filteredCmds.map((cmd, i) => (
                      <button
                        key={cmd.id}
                        onClick={() => insertCommand(cmd)}
                        onMouseEnter={() => setSelectedCmd(i)}
                        role="option"
                        aria-selected={i === selectedCmd}
                        className={cn(
                          "flex w-full items-center gap-3 px-3 py-2.5 text-left text-sm transition-colors",
                          i === selectedCmd ? "bg-accent text-accent-foreground" : "text-popover-foreground hover:bg-accent/50"
                        )}
                      >
                        <span className={cn("flex h-8 w-8 items-center justify-center rounded-sm", i === selectedCmd ? "bg-primary text-primary-foreground" : "bg-muted text-muted-foreground")}>
                          {cmd.icon}
                        </span>
                        <div>
                          <div className="font-medium">{cmd.label}</div>
                          <div className={cn("text-xs", i === selectedCmd ? "text-accent-foreground/70" : "text-muted-foreground")}>{cmd.description}</div>
                        </div>
                      </button>
                    ))
                  )}
                </div>
              </motion.div>
            )}
          </AnimatePresence>

          {/* Validation error */}
          {inputError && (
            <div id="input-error" role="alert" className="px-4 pb-2 text-xs text-destructive">
              {inputError}
            </div>
          )}

          {/* Toolbar */}
          <div className="flex items-center justify-between border-t border-border px-2 py-2">
            <div className="flex items-center gap-1">
              {/* Slash command hint */}
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8 text-muted-foreground active:scale-95"
                    onClick={() => {
                      const next = input + "/";
                      setInput(next);
                      persistDraft(next);
                      setShowSlashMenu(true);
                      setSlashQuery("");
                      setSelectedCmd(0);
                      textareaRef.current?.focus();
                    }}
                    aria-label="Open slash commands"
                    tabIndex={-1}
                  >
                    <Slash className="h-4 w-4" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent><p>Slash commands</p></TooltipContent>
              </Tooltip>

              {/* Attachment button */}
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8 text-muted-foreground active:scale-95"
                    onClick={openFilePicker}
                    aria-label="Attach file"
                    tabIndex={-1}
                    disabled={isStreaming}
                  >
                    <Paperclip className="h-4 w-4" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent><p>Attach file</p></TooltipContent>
              </Tooltip>
              {/* Hidden dropzone input */}
              <input {...getInputProps()} />
            </div>

            {/* Generation status label */}
            {isStreaming && (
              <span className="text-xs text-muted-foreground animate-pulse select-none">
                Generating…
              </span>
            )}

            {/* Mode toggle (Instant / Thinking) */}
            <div className="flex flex-col items-end gap-1">
              <div className="flex items-center gap-1">
                <div
                  role="radiogroup"
                  aria-label="Chat mode"
                  className="flex h-8 items-center rounded-sm border border-input text-xs overflow-hidden"
                >
                  <button
                    type="button"
                    role="radio"
                    aria-checked={effectiveChatMode === "instant"}
                    disabled={!instantHealthy || isStreaming}
                    onClick={() => setStoredChatMode("instant")}
                    title={
                      !instantHealthy
                        ? "Instant mode unavailable (LM Studio unreachable)"
                        : isFallenBack && desiredChatMode === "thinking"
                          ? "Instant — auto-selected because Thinking is unavailable"
                          : "Instant — fast, lightweight model"
                    }
                    className={cn(
                      "h-full px-3 transition-colors",
                      effectiveChatMode === "instant"
                        ? "bg-primary text-primary-foreground"
                        : "text-muted-foreground hover:text-foreground hover:bg-accent",
                      (!instantHealthy || isStreaming) && "opacity-50 cursor-not-allowed",
                    )}
                  >
                    Instant
                  </button>
                  <button
                    type="button"
                    role="radio"
                    aria-checked={effectiveChatMode === "thinking"}
                    disabled={!thinkingHealthy || isStreaming}
                    onClick={() => setStoredChatMode("thinking")}
                    title={
                      !thinkingHealthy
                        ? "Thinking mode unavailable (backend unreachable)"
                        : isFallenBack && desiredChatMode === "instant"
                          ? "Thinking — auto-selected because Instant is unavailable"
                          : "Thinking — full-quality model"
                    }
                    className={cn(
                      "h-full px-3 border-l border-input transition-colors",
                      effectiveChatMode === "thinking"
                        ? "bg-primary text-primary-foreground"
                        : "text-muted-foreground hover:text-foreground hover:bg-accent",
                      (!thinkingHealthy || isStreaming) && "opacity-50 cursor-not-allowed",
                    )}
                  >
                    Thinking
                  </button>
                </div>

                {/* Temperature */}
                <Select
                  value={String(temperature)}
                  onValueChange={(v) => setTemperature(parseFloat(v))}
                  disabled={isStreaming}
                >
                  <SelectTrigger
                    className="h-8 w-[70px] text-xs"
                    aria-label="Temperature"
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {[0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 2.0].map((t) => (
                      <SelectItem key={t} value={String(t)}>
                        {t.toFixed(1)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>

                {/* Retrieval mode */}
                <Select
                  value={retrievalMode}
                  onValueChange={(v) => setRetrievalMode(v as "auto" | "semantic" | "keyword")}
                  disabled={isStreaming}
                >
                  <SelectTrigger
                    className="h-8 w-[90px] text-xs"
                    aria-label="Retrieval mode"
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="auto">Auto</SelectItem>
                    <SelectItem value="semantic">Semantic</SelectItem>
                    <SelectItem value="keyword">Keyword</SelectItem>
                  </SelectContent>
                </Select>

                {/* Citation mode */}
                <Select
                  value={citationMode}
                  onValueChange={(v) => setCitationMode(v as "enabled" | "disabled" | "required")}
                  disabled={isStreaming}
                >
                  <SelectTrigger
                    className="h-8 w-[95px] text-xs"
                    aria-label="Citation mode"
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="enabled">Cite: On</SelectItem>
                    <SelectItem value="disabled">Cite: Off</SelectItem>
                    <SelectItem value="required">Cite: Req</SelectItem>
                  </SelectContent>
                </Select>

                {/* Metadata filter (issue #510 AC-16) — retrieval date/tags/author constraints.
                    Compact inputs matching the selector row's h-8 text-xs visual language. */}
                <Input
                  type="date"
                  value={metadataFilterDateFrom}
                  onChange={(e) => setMetadataFilterDateFrom(e.target.value)}
                  disabled={isStreaming}
                  aria-label="Filter from date"
                  title="Filter sources from this date"
                  className="h-8 w-[118px] text-xs px-2"
                />
                <Input
                  type="date"
                  value={metadataFilterDateTo}
                  onChange={(e) => setMetadataFilterDateTo(e.target.value)}
                  disabled={isStreaming}
                  aria-label="Filter to date"
                  title="Filter sources up to this date"
                  className="h-8 w-[118px] text-xs px-2"
                />
                <Input
                  type="text"
                  value={metadataFilterTags}
                  onChange={(e) => setMetadataFilterTags(e.target.value)}
                  disabled={isStreaming}
                  aria-label="Filter tags"
                  placeholder="Tags"
                  title="Comma-separated source tags"
                  className="h-8 w-[90px] text-xs px-2"
                />
                <Input
                  type="text"
                  value={metadataFilterAuthor}
                  onChange={(e) => setMetadataFilterAuthor(e.target.value)}
                  disabled={isStreaming}
                  aria-label="Filter author"
                  placeholder="Author"
                  title="Filter by source author"
                  className="h-8 w-[90px] text-xs px-2"
                />
              </div>
              {isFallenBack && (
                <span
                  role="status"
                  aria-live="polite"
                  className="text-[10px] text-amber-700 dark:text-amber-300"
                >
                  Using {effectiveChatMode} — {desiredChatMode} unavailable
                </span>
              )}
            </div>

            {/* Send / Stop */}
            {isStreaming ? (
              <Button
                variant="destructive"
                size="sm"
                onClick={onStop}
                className="gap-1.5 h-8 active:scale-95"
                aria-label="Stop generating"
              >
                <Square className="h-3 w-3 fill-current" />
                Stop
              </Button>
            ) : (
              <Button
                size="sm"
                onClick={handleSubmit}
                disabled={!input.trim() || input.length > MAX_INPUT_LENGTH || hasUploading}
                className="h-8 w-8 rounded-full p-0 shadow-xs active:scale-95"
                aria-label="Send message"
              >
                <Send className="h-3.5 w-3.5" />
              </Button>
            )}
          </div>
        </div>

        {/* Character count */}
        {input.length > MAX_INPUT_LENGTH * 0.75 && (
          <div
            className={cn(
              "mt-1 text-right text-[11px]",
              input.length > MAX_INPUT_LENGTH ? "text-destructive" : "text-muted-foreground"
            )}
            aria-live="polite"
          >
            {input.length}/{MAX_INPUT_LENGTH}
          </div>
        )}
      </div>
    </TooltipProvider>
  );
}
