// frontend/src/components/chat/SessionRail.tsx
// SessionRail component with full business logic for chat session management

import {
  useState,
  useCallback,
  useMemo,
  useEffect,
  useRef,
  useLayoutEffect,
  useSyncExternalStore,
  forwardRef,
} from "react";
import type { MutableRefObject } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useDebounce } from "@/hooks/useDebounce";
import { useLocation, useNavigate } from "react-router-dom";
import {
  MessageSquare,
  Search,
  Pin,
  PinOff,
  Pencil,
  Trash2,
  MoreHorizontal,
  Plus,
  X,
  Check,
  ChevronDown,
  ChevronRight,
  GitBranch,
  AlertCircle,
} from "lucide-react";
import { formatRelativeTime } from "@/lib/formatters";
import { dispatchCommandPaletteOpen } from "@/lib/commandPaletteEvents";
import {
  useCommandPaletteAction,
  type CommandPaletteActionGuard,
} from "@/lib/commandPaletteActions";
import { useAuthOwner } from "@/hooks/useAuthOwner";
import {
  captureAuthPrincipalGeneration,
  isCurrentAuthOwner,
  subscribeAuthPrincipal,
  type AuthOwner,
} from "@/lib/api/auth-lifecycle";
import {
  comboFromEvent,
  effectiveBinding,
  isEditableTarget,
} from "@/lib/shortcutBindings";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { useChatStore } from "@/stores/useChatStore";
import { useVaultStore } from "@/stores/useVaultStore";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { toast } from "sonner";
import {
  listChatSessions,
  deleteChatSession,
  updateChatSession,
  getChatSession,
  type ChatSession,
  type ChatSessionDetail,
} from "@/lib/api";
import { useTestMode } from "@/fixtures/TestModeContext";
import { mockChatSessions } from "@/fixtures/chat";
import { HugeiconsIcon } from "@hugeicons/react";
import { Message01Icon } from "@hugeicons/core-free-icons";

// =============================================================================
// TYPES & INTERFACES
// =============================================================================

interface ChatSearchInputProps {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  className?: string;
}

interface SessionGroupProps {
  label: string;
  sessions: ChatSession[];
  isOpen?: boolean;
  onToggle?: () => void;
  activeSessionId: string | null;
  onSessionClick: (session: ChatSession) => void;
  onSessionRename: (session: ChatSession, newTitle: string) => void;
  onSessionPinToggle: (sessionId: number) => void;
  onSessionDelete: (session: ChatSession) => void;
  isSessionPinned: (sessionId: number) => boolean;
  focusedIndex: number;
  onFocusedIndexChange: (index: number) => void;
  indexOffset: number;
  className?: string;
}

interface SessionItemProps {
  session: ChatSession;
  isActive: boolean;
  isPinned: boolean;
  onClick: () => void;
  onRename: (newTitle: string) => void;
  onPinToggle: () => void;
  onDelete: () => void;
  tabIndex?: number;
  onKeyDown?: (e: React.KeyboardEvent) => void;
}

// Time-based group keys
type TimeGroupKey = "pinned" | "today" | "yesterday" | "thisWeek" | "older";

// =============================================================================
// UTILITY FUNCTIONS
// =============================================================================

/**
 * Get time group for a date string
 */
function getTimeGroup(dateStr: string): "Today" | "Yesterday" | "This Week" | "Older" {
  const date = new Date(dateStr);
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const yesterday = new Date(today);
  yesterday.setDate(yesterday.getDate() - 1);
  const weekAgo = new Date(today);
  weekAgo.setDate(weekAgo.getDate() - 7);

  if (date >= today) return "Today";
  if (date >= yesterday) return "Yesterday";
  if (date >= weekAgo) return "This Week";
  return "Older";
}

/**
 * Group sessions by time period
 */
function groupSessionsByTime(
  sessions: ChatSession[],
  pinnedIds: number[]
): Record<TimeGroupKey, ChatSession[]> {
  const groups: Record<TimeGroupKey, ChatSession[]> = {
    pinned: [],
    today: [],
    yesterday: [],
    thisWeek: [],
    older: [],
  };

  sessions.forEach((session) => {
    if (pinnedIds.includes(session.id)) {
      groups.pinned.push(session);
    } else {
      const timeGroup = getTimeGroup(session.updated_at);
      const timeKey: TimeGroupKey =
        timeGroup === "Today"
          ? "today"
          : timeGroup === "Yesterday"
          ? "yesterday"
          : timeGroup === "This Week"
          ? "thisWeek"
          : "older";
      groups[timeKey].push(session);
    }
  });

  // Sort each group by updated_at descending
  (Object.keys(groups) as TimeGroupKey[]).forEach((key) => {
    groups[key].sort(
      (a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime()
    );
  });

  return groups;
}

// =============================================================================
// COMPONENT: ChatSearchInput
// =============================================================================

function canFocusChatSearch(input: HTMLInputElement): boolean {
  if (!input.isConnected || input.disabled) return false;
  for (let element: HTMLElement | null = input; element; element = element.parentElement) {
    if (element.hidden || element.getAttribute("aria-hidden") === "true") return false;
    const isExplicitlyCollapsedRail =
      element.style.width === "0px" &&
      element.classList.contains("md:w-0") &&
      element.classList.contains("md:overflow-hidden");
    if (isExplicitlyCollapsedRail) return false;
    const style = window.getComputedStyle?.(element);
    if (style?.display === "none" || style?.visibility === "hidden" || style?.opacity === "0") {
      return false;
    }
  }
  return true;
}

export function ChatSearchInput({
  value,
  onChange,
  placeholder = "Search sessions...",
  className,
}: ChatSearchInputProps) {
  const inputRef = useRef<HTMLInputElement>(null);

  // Focus-search shortcut (default Ctrl/Cmd+K; rebindable per-browser via
  // issue #573 AC4 — the effective binding is read at event time so a
  // persisted override applies on every mount).
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.repeat || e.isComposing) return;
      const combo = comboFromEvent(e);
      if (combo === null || combo !== effectiveBinding("focusSearch", "Ctrl+K")) return;
      // Printable bindings do not run from an editor. Modifier bindings such
      // as Ctrl+K retain the chat search behavior from editable surfaces.
      if (isEditableTarget(e.target) && !e.ctrlKey && !e.metaKey) return;
      const input = inputRef.current;
      if (!input || !canFocusChatSearch(input)) return;
      // ChatShell keeps the desktop rail mounted while hidden. Do not claim
      // the event from that hidden instance when the browser exposes the
      // visibility check; older browsers and jsdom retain the prior behavior.
      if (
        typeof input.checkVisibility === "function" &&
        !input.checkVisibility({ checkOpacity: true })
      ) {
        return;
      }
      input.focus();
      if (document.activeElement !== input) return;
      e.preventDefault();
      e.stopPropagation();
    };
    document.addEventListener("keydown", handleKeyDown, true);
    return () => document.removeEventListener("keydown", handleKeyDown, true);
  }, []);

  const handleClear = useCallback(() => {
    onChange("");
    inputRef.current?.focus();
  }, [onChange]);

  return (
    <div className={className}>
      <div className="relative">
        <Search
          className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
          aria-hidden="true"
        />
        <Input
          ref={inputRef}
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          className="h-9 pl-9 pr-16 text-sm"
          aria-label="Search chat sessions"
        />
        <div className="absolute right-2 top-1/2 -translate-y-1/2 flex items-center gap-1">
          {value && (
            <Button
              variant="ghost"
              size="icon"
              className="h-6 w-6"
              onClick={handleClear}
              aria-label="Clear search"
            >
              <X className="h-3 w-3" aria-hidden="true" />
            </Button>
          )}
          <kbd className="hidden sm:inline-flex h-5 select-none items-center gap-1 rounded-sm border bg-muted px-1.5 font-mono text-[10px] font-medium text-muted-foreground">
            <span className="text-xs">Ctrl</span>K
          </kbd>
        </div>
      </div>
    </div>
  );
}

// =============================================================================
// COMPONENT: SessionItem
// =============================================================================

export const SessionItem = forwardRef<HTMLDivElement, SessionItemProps>(
  function SessionItem(
    {
      session,
      isActive,
      isPinned,
      onClick,
      onRename,
      onPinToggle,
      onDelete,
      tabIndex,
      onKeyDown,
    }: SessionItemProps,
    ref: React.Ref<HTMLDivElement>
  ) {
  const [isEditing, setIsEditing] = useState(false);
  const [editTitle, setEditTitle] = useState(session.title || "");
  const inputRef = useRef<HTMLInputElement>(null);

  const displayTitle = session.title || "Untitled";
  const truncatedTitle =
    displayTitle.length > 40 ? displayTitle.slice(0, 40) + "..." : displayTitle;

  const handleStartEdit = useCallback(() => {
    setEditTitle(session.title || "");
    setIsEditing(true);
  }, [session.title]);

  const handleSaveEdit = useCallback(() => {
    const trimmed = editTitle.trim();
    // Don't close edit mode if trimmed is empty - let user try again
    if (!trimmed) return;
    if (trimmed !== session.title) {
      onRename(trimmed);
    }
    setIsEditing(false);
  }, [editTitle, session.title, onRename]);

  const handleCancelEdit = useCallback(() => {
    setEditTitle(session.title || "");
    setIsEditing(false);
  }, [session.title]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Enter") {
        // Mark handled so window-level shortcuts don't also react to this key.
        e.preventDefault();
        handleSaveEdit();
      } else if (e.key === "Escape") {
        // Cancel title editing only. preventDefault stops this Escape from
        // bubbling to the window-scoped Esc-to-stop handler (useEscapeToStop,
        // which defers to !e.defaultPrevented), so cancelling a rename never
        // also aborts an in-flight streaming response.
        e.preventDefault();
        handleCancelEdit();
      }
    },
    [handleSaveEdit, handleCancelEdit]
  );

  // Focus input when editing starts
  useEffect(() => {
    if (isEditing) {
      inputRef.current?.focus();
      inputRef.current?.select();
    }
  }, [isEditing]);

  return (
    <>
      <div
        ref={ref}
        className={`
          group relative flex flex-col justify-start items-start rounded-sm px-3 py-2.5 text-sm mb-2
          cursor-pointer transition-all duration-150 ease-in-out
          border border-transparent
          ${isActive ? "bg-primary/10 border-ring" : "hover:bg-muted hover:border-border/50"}
          focus-within:border focus-within:border-ring
        `}
        onClick={() => !isEditing && onClick()}
        role={isEditing ? "listitem" : "button"}
        tabIndex={isEditing ? undefined : (tabIndex ?? -1)}
        aria-label={isEditing ? undefined : `Chat session: ${displayTitle}`}
        onKeyDown={isEditing ? undefined : (e) => {
          // First handle the parent's roving tabindex navigation
          onKeyDown?.(e);
          // Then handle activation with Enter or Space
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onClick();
          }
        }}
      >
        <div className="flex flex-col items-start gap-2">
          <div className="flex items-center justify-between gap-2">
            <HugeiconsIcon icon={Message01Icon} strokeWidth={1.2} size={16} className={isActive ? "text-accent-foreground" : "text-muted-foreground"} aria-hidden="true" />

            <div className="w-full">
              {isEditing ? (
                <div className="flex items-center gap-2">
                  <Input
                    ref={inputRef}
                    type="text"
                    value={editTitle}
                    onChange={(e) => setEditTitle(e.target.value)}
                    onKeyDown={handleKeyDown}
                    onClick={(e) => e.stopPropagation()}
                    className="flex-1 h-6 px-1 text-sm"
                    aria-label="Edit session title"
                  />
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-6 w-6"
                    onClick={(e) => {
                      e.stopPropagation();
                      handleSaveEdit();
                    }}
                    aria-label="Save title"
                  >
                    <Check className="h-3 w-3" aria-hidden="true" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-6 w-6"
                    onClick={(e) => {
                      e.stopPropagation();
                      handleCancelEdit();
                    }}
                    aria-label="Cancel editing"
                  >
                    <X className="h-3 w-3" aria-hidden="true" />
                  </Button>
                </div>
              ) : (
                <div className="flex items-center justify-between gap-2">
                  <span className={`truncate font-medium${isActive ? " text-foreground" : ""}`}>{truncatedTitle}</span>
                  {session.forked_from_session_id != null && (
                    <span title="Branched conversation"><GitBranch className="h-3 w-3 text-muted-foreground shrink-0" aria-hidden="true" /></span>
                  )}
                  {isPinned && (
                    <Pin className="h-3 w-3 text-muted-foreground shrink-0" aria-hidden="true" />
                  )}
                  {!isEditing && (
                    <div className="justify-items-end">
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7"
                          onClick={(e) => e.stopPropagation()}
                          aria-label="More options"
                        >
                          <MoreHorizontal className="h-3.5 w-3.5" aria-hidden="true" />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end" className="w-48">
                        <DropdownMenuItem
                          aria-label={isPinned ? "Unpin session" : "Pin session"}
                          onClick={(e) => {
                            e.stopPropagation();
                            onPinToggle();
                          }}
                        >
                          {isPinned ? (
                            <>
                              <PinOff className="mr-2 h-4 w-4" aria-hidden="true" />
                              Unpin
                            </>
                          ) : (
                            <>
                              <Pin className="mr-2 h-4 w-4" aria-hidden="true" />
                              Pin
                            </>
                          )}
                        </DropdownMenuItem>
                        <DropdownMenuItem
                          aria-label="Rename session"
                          onClick={(e) => {
                            e.stopPropagation();
                            handleStartEdit();
                          }}
                        >
                          <Pencil className="mr-2 h-4 w-4" aria-hidden="true" />
                          Rename
                        </DropdownMenuItem>
                        <DropdownMenuSeparator />
                        <DropdownMenuItem
                          aria-label="Delete session"
                          onClick={(e) => {
                            e.stopPropagation();
                            onDelete();
                          }}
                          className="text-destructive focus:text-destructive bg-destructive/10 hover:bg-destructive/20!"
                        >
                          <Trash2 className="mr-2 h-4 w-4" aria-hidden="true" />
                          Delete
                        </DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>

          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <span>{formatRelativeTime(session.updated_at)}</span>
            {session.message_count !== undefined && (
              <>
                <span aria-hidden="true">&mdash;</span>
                <span>{session.message_count} messages</span>
              </>
            )}
          </div>
        </div>
      </div>
    </>
  );
});

// =============================================================================
// COMPONENT: SessionGroup
// =============================================================================

export function SessionGroup({
  label,
  sessions,
  isOpen: controlledIsOpen,
  onToggle,
  activeSessionId,
  onSessionClick,
  onSessionRename,
  onSessionPinToggle,
  onSessionDelete,
  isSessionPinned,
  focusedIndex,
  onFocusedIndexChange,
  indexOffset,
  className,
}: SessionGroupProps) {
  const [internalIsOpen, setInternalIsOpen] = useState(true);
  const isOpen = controlledIsOpen ?? internalIsOpen;
  const itemRefs = useRef<(HTMLDivElement | null)[]>([]);

  const handleToggle = useCallback(() => {
    if (onToggle) {
      onToggle();
    } else {
      setInternalIsOpen((prev) => !prev);
    }
  }, [onToggle]);

  // Keyboard navigation handler for roving tabindex
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (sessions.length === 0) return;

      if (e.key === "ArrowDown") {
        e.preventDefault();
        const currentLocalIndex = focusedIndex - indexOffset;
        const nextLocalIndex = Math.min(currentLocalIndex + 1, sessions.length - 1);
        onFocusedIndexChange(indexOffset + nextLocalIndex);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        const currentLocalIndex = focusedIndex - indexOffset;
        const prevLocalIndex = Math.max(currentLocalIndex - 1, 0);
        onFocusedIndexChange(indexOffset + prevLocalIndex);
      } else if (e.key === "Home") {
        e.preventDefault();
        onFocusedIndexChange(indexOffset);
      } else if (e.key === "End") {
        e.preventDefault();
        onFocusedIndexChange(indexOffset + sessions.length - 1);
      }
    },
    [focusedIndex, sessions.length, onFocusedIndexChange, indexOffset]
  );

  // Move DOM focus when focusedIndex changes
  useLayoutEffect(() => {
    const localIndex = focusedIndex - indexOffset;
    if (localIndex >= 0 && localIndex < sessions.length) {
      itemRefs.current[localIndex]?.focus();
    }
  }, [focusedIndex, sessions.length, indexOffset]);

  if (sessions.length === 0) {
    return null;
  }

  return (
    <div className={className}>
      <button
        type="button"
        onClick={handleToggle}
        className="flex w-full items-center justify-between px-2 py-1.5 text-xs font-semibold text-muted-foreground uppercase tracking-wide hover:text-foreground transition-colors"
        aria-expanded={isOpen}
        aria-label={`${label} section, ${sessions.length} sessions`}
      >
        <div className="flex items-center gap-2">
          {isOpen ? (
            <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5" aria-hidden="true" />
          )}
          <span>{label}</span>
        </div>
        <Badge variant="secondary" className="text-[10px] h-4 px-1.5">
          {sessions.length}
        </Badge>
      </button>

      {isOpen && (
        // eslint-disable-next-line jsx-a11y-x/no-noninteractive-element-interactions -- role="list" container uses event delegation for roving-tabindex arrow-key navigation; each child SessionItem also has its own onKeyDown (lines 572+). APG listbox pattern.
        <div
          className="mt-1 space-y-0.5"
          role="list"
          aria-label={`${label} sessions`}
          onKeyDown={handleKeyDown}
        >
          {sessions.map((session, index) => (
            <SessionItem
              key={session.id}
              session={session}
              isActive={String(session.id) === activeSessionId}
              isPinned={isSessionPinned(session.id)}
              onClick={() => {
                onSessionClick(session);
                onFocusedIndexChange(indexOffset + index);
                itemRefs.current[index]?.focus();
              }}
              onRename={(newTitle) => onSessionRename(session, newTitle)}
              onPinToggle={() => onSessionPinToggle(session.id)}
              onDelete={() => onSessionDelete(session)}
              tabIndex={index + indexOffset === focusedIndex ? 0 : -1}
              ref={(el: HTMLDivElement | null) => {
                itemRefs.current[index] = el;
              }}
              onKeyDown={(e: React.KeyboardEvent) => {
                // Arrow keys are handled by the list container
                if (e.key === "ArrowUp" || e.key === "ArrowDown") {
                  e.preventDefault();
                }
              }}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// =============================================================================
// COMPONENT: SessionRail (Main)
// =============================================================================

// RT-08 fix: Module-level cache to deduplicate session list fetches across
// multiple SessionRail instances (desktop sidebar + mobile sheet)
// Exported for test reset between test runs — not part of the public API.
export const _sessionCache: {
  data: ChatSession[] | null;
  vaultId?: number;
  owner?: AuthOwner;
  principalGeneration?: number;
  readAttempt?: number;
  ts: number;
} = {
  data: null,
  ts: 0,
};
const SESSION_CACHE_TTL = 5000; // 5 seconds

interface SessionContext {
  // A private lease distinguishes A -> B -> A from the first A.
  readonly lease: number;
  readonly owner: AuthOwner;
  readonly principalGeneration: number;
  readonly vaultId?: number;
}

interface PendingDelete {
  readonly token: number;
  readonly session: ChatSession;
  readonly context: SessionContext;
  readonly wasActive: boolean;
  readonly wasPinned: boolean;
  readonly timer: ReturnType<typeof setTimeout>;
  undone: boolean;
  finalized: boolean;
}

function sameSessionContext(left: SessionContext, right: SessionContext): boolean {
  // Cache equivalence may compare fields; callback liveness must be exact.
  return left === right;
}

function isCurrentSessionContext(context: SessionContext, currentVaultId?: number): boolean {
  return (
    isCurrentAuthOwner(context.owner) &&
    captureAuthPrincipalGeneration() === context.principalGeneration &&
    context.vaultId === currentVaultId
  );
}

function isFreshSessionCache(context: SessionContext): boolean {
  const contextMatches =
    _sessionCache.owner === context.owner &&
    _sessionCache.principalGeneration === context.principalGeneration &&
    _sessionCache.vaultId === context.vaultId;
  return Boolean(
    _sessionCache.data &&
      contextMatches &&
      Date.now() - _sessionCache.ts < SESSION_CACHE_TTL
  );
}

function SessionRailPaletteAction({
  resetNewChat,
}: {
  resetNewChat: (guard?: CommandPaletteActionGuard) => void;
}) {
  useCommandPaletteAction({
    id: "new-chat",
    label: "New chat",
    enabled: true,
    execute: (guard) => resetNewChat(guard),
  });
  return null;
}

interface SessionRailProps {
  vaultId?: number;
  className?: string;
}

export function SessionRail({ vaultId, className }: SessionRailProps) {
  const authOwner = useAuthOwner();
  const principalGeneration = useSyncExternalStore(
    subscribeAuthPrincipal,
    captureAuthPrincipalGeneration,
    captureAuthPrincipalGeneration,
  );
  const leaseRef = useRef(0);
  const sessionContext = useMemo<SessionContext>(
    () => ({ lease: ++leaseRef.current, owner: authOwner, principalGeneration, vaultId }),
    [authOwner, principalGeneration, vaultId],
  );
  const latestSessionContextRef = useRef(sessionContext);
  const rowTokenRef = useRef<Map<number, number>>(new Map());
  const pendingDeletesRef = useRef<Map<number, PendingDelete>>(new Map());
  useLayoutEffect(() => {
    latestSessionContextRef.current = sessionContext;
  }, [sessionContext]);
  return (
    <SessionRailContent
      key={sessionContext.lease}
      vaultId={vaultId}
      className={className}
      sessionContext={sessionContext}
      latestSessionContextRef={latestSessionContextRef}
      rowTokenRef={rowTokenRef}
      pendingDeletesRef={pendingDeletesRef}
    />
  );
}

function SessionRailContent({
  vaultId,
  className,
  sessionContext,
  latestSessionContextRef,
  rowTokenRef,
  pendingDeletesRef,
}: SessionRailProps & {
  sessionContext: SessionContext;
  latestSessionContextRef: MutableRefObject<SessionContext>;
  rowTokenRef: MutableRefObject<Map<number, number>>;
  pendingDeletesRef: MutableRefObject<Map<number, PendingDelete>>;
}) {
  const testMode = useTestMode();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const mountedRef = useRef(false);
  const invocationLeaseRef = useRef<{ active: boolean } | null>(null);
  const sessionReadAttemptRef = useRef(0);
  const detailReadAttemptRef = useRef(0);
  const previousSessionContextRef = useRef<SessionContext | null>(null);
  const pendingRenameRef = useRef<Map<number, symbol>>(new Map());
  const {
    activeSessionId,
    sessionSearchQuery,
    pinnedSessionIds,
    setSessionSearchQuery,
    togglePinSession,
    isSessionPinned,
    setActiveSessionId,
    setActiveSessionTitle,
    sessionListRefreshToken,
  } = useChatShellStore();
  const { getActiveVault: _getActiveVault } = useVaultStore();
  const activeVault = _getActiveVault();

  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [sessionDetails, setSessionDetails] = useState<Map<number, ChatSessionDetail>>(new Map());
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [focusedSessionIndex, setFocusedSessionIndex] = useState(0);
  useEffect(() => {
    mountedRef.current = true;
    const invocationLease = { active: true };
    invocationLeaseRef.current = invocationLease;
    const pendingDeletes = pendingDeletesRef.current;
    const rowTokens = rowTokenRef.current;
    const pendingRename = pendingRenameRef.current;
    return () => {
      mountedRef.current = false;
      invocationLease.active = false;
      for (const [sessionId] of rowTokens) {
        if (![...pendingDeletes.values()].some((pending) => pending.session.id === sessionId)) {
          rowTokens.delete(sessionId);
        }
      }
      pendingRename.clear();
    };
  }, [pendingDeletesRef, rowTokenRef]);

  // H-7 fix: Debounce search to avoid firing API calls per keystroke
  const [debouncedSearchQuery] = useDebounce(sessionSearchQuery, 300);
  const detailQueryContext = useMemo(
    () => ({ raw: sessionSearchQuery, debounced: debouncedSearchQuery }),
    [sessionSearchQuery, debouncedSearchQuery],
  );
  const latestDetailQueryContextRef = useRef(detailQueryContext);
  latestDetailQueryContextRef.current = detailQueryContext;

  // Session detail markers and content belong to the same private context as
  // the list. A principal or vault transition retires both before the next
  // read can publish.
  const fetchedIdsRef = useRef<Set<number>>(new Set());
  useEffect(() => {
    const previous = previousSessionContextRef.current;
    if (previous && !sameSessionContext(previous, sessionContext)) {
      fetchedIdsRef.current.clear();
      setSessions([]);
      setSessionDetails(new Map());
    }
    previousSessionContextRef.current = sessionContext;
  }, [sessionContext]);

  // Fetch sessions on mount and when vaultId changes (with dedup cache)
  const lastRefreshTokenRef = useRef(sessionListRefreshToken);
  useEffect(() => {
    let cancelled = false;
    const readContext = sessionContext;
    const invocationLease = invocationLeaseRef.current;
    if (!mountedRef.current || !isCurrentSessionContext(readContext, vaultId) || !invocationLease?.active) return;
    const readAttempt = ++sessionReadAttemptRef.current;
    const forceRefresh = sessionListRefreshToken !== lastRefreshTokenRef.current;
    lastRefreshTokenRef.current = sessionListRefreshToken;
    const canPublish = () =>
      !cancelled &&
      mountedRef.current &&
      invocationLease.active && invocationLeaseRef.current === invocationLease &&
      readAttempt === sessionReadAttemptRef.current &&
      sameSessionContext(readContext, latestSessionContextRef.current) &&
      isCurrentSessionContext(readContext, latestSessionContextRef.current.vaultId);
    const fetchSessions = async () => {
      if (testMode) {
        if (canPublish()) {
          setSessions((previous) => canPublish() ? mockChatSessions : previous);
          setIsLoading((previous) => canPublish() ? false : previous);
        }
        return;
      }
      // Use cache if fresh and same vault
      if (!forceRefresh && isFreshSessionCache(readContext) && canPublish()) {
        const cached = _sessionCache.data ?? [];
        setSessions((previous) => canPublish() ? cached : previous);
        setIsLoading((previous) => canPublish() ? false : previous);
        return;
      }

      if (canPublish()) {
        setIsLoading((previous) => canPublish() ? true : previous);
        setError((previous) => canPublish() ? null : previous);
      }
      try {
        // Physical reads from separate rail instances share this monotonic
        // identity so an older response cannot overwrite the newer cache.
        if (!canPublish()) return;
        const cacheReadAttempt = (_sessionCache.readAttempt ?? 0) + 1;
        _sessionCache.readAttempt = cacheReadAttempt;
        if (!canPublish()) return;
        const data = await listChatSessions(vaultId);
        if (!data || !Array.isArray(data.sessions)) {
          throw new Error("Session list response was malformed");
        }
        const sessionList = Array.isArray(data.sessions) ? data.sessions : [];
        if (!canPublish()) return;
        if (_sessionCache.readAttempt === cacheReadAttempt) {
          _sessionCache.data = sessionList;
          _sessionCache.vaultId = vaultId;
          _sessionCache.owner = readContext.owner;
          _sessionCache.principalGeneration = readContext.principalGeneration;
          _sessionCache.ts = Date.now();
        }
        setSessions((previous) => (canPublish() ? sessionList : previous));
      } catch (err) {
        const message = err instanceof Error ? err.message : "Failed to load sessions";
        if (canPublish()) setError((previous) => canPublish() ? message : previous);
      } finally {
        if (canPublish()) setIsLoading((previous) => canPublish() ? false : previous);
      }
    };

    fetchSessions();
    return () => { cancelled = true; };
  }, [vaultId, sessionListRefreshToken, testMode, sessionContext, latestSessionContextRef]);

  // Fetch session details for first message content when needed for search
  useEffect(() => {
    let cancelled = false;
    const readContext = sessionContext;
    const invocationLease = invocationLeaseRef.current;
    if (!mountedRef.current || !isCurrentSessionContext(readContext, vaultId) || !invocationLease?.active) return;
    const readAttempt = ++detailReadAttemptRef.current;
    const queryContext = detailQueryContext;
    const query = queryContext.debounced;
    const canPublish = () =>
      !cancelled &&
      mountedRef.current &&
      invocationLease.active && invocationLeaseRef.current === invocationLease &&
      readAttempt === detailReadAttemptRef.current &&
      latestDetailQueryContextRef.current === queryContext &&
      queryContext.raw === queryContext.debounced &&
      sameSessionContext(readContext, latestSessionContextRef.current) &&
      isCurrentSessionContext(readContext, latestSessionContextRef.current.vaultId);
    const sessionsNeedingDetails = sessions
      .filter((session) => !fetchedIdsRef.current.has(session.id))
      .slice(0, 50);
    if (!query.trim() || queryContext.raw !== queryContext.debounced || sessionsNeedingDetails.length === 0) {
      return () => { cancelled = true; };
    }

    const fetchSessionDetails = async () => {
      const results = await Promise.all(
        sessionsNeedingDetails.map(async (session) => {
          try {
            if (!canPublish()) return null;
            const detail = await getChatSession(session.id);
            if (!canPublish()) return null;
            return [session.id, detail] as const;
          } catch {
            return null;
          }
        }),
      );
      if (!canPublish()) return;
      const successful = results.filter(
        (result): result is readonly [number, ChatSessionDetail] => result !== null,
      );
      if (successful.length === 0) return;
      setSessionDetails((previous) => {
        if (!canPublish()) return previous;
        const next = new Map(previous);
        for (const [id, detail] of successful) next.set(id, detail);
        return next;
      });
      // Fetched markers follow committed details in the layout effect below;
      // a queued result retired before commit must not poison future reads.
    };

    fetchSessionDetails();
    return () => { cancelled = true; };
  }, [debouncedSearchQuery, detailQueryContext, sessions, sessionContext, vaultId, latestSessionContextRef]);

  useLayoutEffect(() => {
    if (!isCurrentSessionContext(sessionContext, vaultId)) return;
    fetchedIdsRef.current = new Set(sessionDetails.keys());
  }, [sessionDetails, sessionContext, vaultId]);

  // Filter sessions based on search query (title + first message content)
  const filteredSessions = useMemo(() => {
    if (!debouncedSearchQuery.trim()) return sessions;

    const query = debouncedSearchQuery.toLowerCase();
    return sessions.filter((session) => {
      // Search title
      const titleMatch = (session.title || "Untitled").toLowerCase().includes(query);
      
      // Search first message content if available
      const detail = sessionDetails.get(session.id);
      const firstMessageContent = detail?.messages?.[0]?.content || "";
      const contentMatch = firstMessageContent.toLowerCase().includes(query);
      
      return titleMatch || contentMatch;
    });
  }, [sessions, debouncedSearchQuery, sessionDetails]);

  // Reset focused index when filtered list shrinks to prevent out-of-bounds focus
  useEffect(() => {
    setFocusedSessionIndex((prev) =>
      prev >= filteredSessions.length ? Math.max(0, filteredSessions.length - 1) : prev
    );
  }, [filteredSessions.length]);

  // Group sessions by time
  const groupedSessions = useMemo(
    () => groupSessionsByTime(filteredSessions, pinnedSessionIds),
    [filteredSessions, pinnedSessionIds]
  );

  const nextRowToken = useCallback((sessionId: number) => {
    const token = (rowTokenRef.current.get(sessionId) ?? 0) + 1;
    rowTokenRef.current.set(sessionId, token);
    return token;
  }, [rowTokenRef]);
  const isLiveContext = useCallback((context: SessionContext) => {
    return (
      mountedRef.current &&
      sameSessionContext(context, latestSessionContextRef.current) &&
      isCurrentAuthOwner(context.owner) &&
      captureAuthPrincipalGeneration() === context.principalGeneration
    );
  }, [latestSessionContextRef]);
  const releaseDeleteRecord = useCallback((pending: PendingDelete) => {
    if (pendingDeletesRef.current.get(pending.session.id) !== pending) return;
    pendingDeletesRef.current.delete(pending.session.id);
    if (
      !mountedRef.current &&
      rowTokenRef.current.get(pending.session.id) === pending.token &&
      !pendingDeletesRef.current.has(pending.session.id)
    ) {
      rowTokenRef.current.delete(pending.session.id);
    }
  }, [pendingDeletesRef, rowTokenRef]);

  // H-1: Virtualizer refs and setup
  const listRef = useRef<HTMLDivElement>(null);
  const sessionVirtualizer = useVirtualizer({
    count: filteredSessions.length,
    getScrollElement: () => listRef.current,
    estimateSize: () => 64,
    measureElement: (el) => el?.getBoundingClientRect().height ?? 64,
    overscan: 5,
  });

  // UI-045: move DOM focus (and the roving focused index) to the row wrapper
  // carrying data-session-index. SessionItem does not forward unknown props to
  // its focusable root, so the row wrappers own the data attribute and receive
  // programmatic focus.
  const moveSessionFocus = useCallback(
    (targetIndex: number) => {
      const len = filteredSessions.length;
      if (len === 0) return;
      const clamped = Math.max(0, Math.min(targetIndex, len - 1));
      setFocusedSessionIndex(clamped);
      // Scroll the virtual window first so the target row exists in the DOM
      // before we try to focus it (no-op in the non-virtualized fallback).
      // "auto" is this library's minimal-scroll alignment (virtual-core has no
      // "nearest"): no scroll when in view, else end/start.
      if (sessionVirtualizer.getVirtualItems().length > 0) {
        sessionVirtualizer.scrollToIndex(clamped, { align: "auto" });
      }
      requestAnimationFrame(() => {
        // PRR-008: tanstack-virtual recalculates its window in its own rAF, so
        // the target row may not be committed yet on this frame. Retry focus
        // once on the following frame instead of silently dropping it.
        const focusRow = () => {
          const row = listRef.current?.querySelector<HTMLElement>(
            `[data-session-index="${clamped}"]`
          );
          if (!row) return false;
          row.focus();
          row.scrollIntoView({ block: "nearest" });
          return true;
        };
        if (!focusRow()) requestAnimationFrame(focusRow);
      });
    },
    [filteredSessions.length, sessionVirtualizer]
  );

  // UI-045: roving keyboard navigation for the live session lists (ArrowUp/
  // ArrowDown/Home/End, matching the grouped SessionGroup list pattern).
  const handleSessionListKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      const len = filteredSessions.length;
      if (len === 0) return;
      if (e.key === "ArrowDown") {
        e.preventDefault();
        moveSessionFocus(Math.min(focusedSessionIndex + 1, len - 1));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        moveSessionFocus(Math.max(focusedSessionIndex - 1, 0));
      } else if (e.key === "Home") {
        e.preventDefault();
        moveSessionFocus(0);
      } else if (e.key === "End") {
        e.preventDefault();
        moveSessionFocus(len - 1);
      }
    },
    [filteredSessions.length, focusedSessionIndex, moveSessionFocus]
  );

  // Handle new chat
  const resetNewChat = useCallback(
    (guard?: CommandPaletteActionGuard) => {
      const capturedContext = sessionContext;
      const isCurrent = () =>
        (guard?.isCurrent() ?? true) && isLiveContext(capturedContext);
      if (!isCurrent()) return;
      useChatStore.getState().newChat();
      if (!isCurrent()) return;
      setActiveSessionId(null);
      if (!isCurrent()) return;
      setActiveSessionTitle(null);
      if (!isCurrent()) return;
      setSessionSearchQuery("");
      if (!isCurrent()) return;
      navigate("/chat");
    },
    [
      isLiveContext,
      navigate,
      sessionContext,
      setActiveSessionId,
      setActiveSessionTitle,
      setSessionSearchQuery,
    ]
  );

  const handleNewChat = useCallback(() => {
    resetNewChat();
  }, [resetNewChat]);

  // The full palette hook requires an active vault store boundary. Keep it in
  // a private child that exists only on the real chat route; the child remains
  // the first fragment sibling across loading, error, empty, and success UI.
  const paletteAction =
    pathname === "/chat" ? <SessionRailPaletteAction resetNewChat={resetNewChat} /> : null;

  // Handle session click
  const handleSessionClick = useCallback(
    (session: ChatSession) => {
      const capturedContext = sessionContext;
      const isCurrent = () => isLiveContext(capturedContext);
      if (!isCurrent()) return false;
      setActiveSessionId(String(session.id));
      if (!isCurrent()) return false;
      setActiveSessionTitle(session.title || null);
      if (!isCurrent()) return false;
      setSessionSearchQuery("");
      if (!isCurrent()) return false;
      navigate(`/chat/${session.id}`);
      return isCurrent();
    },
    [
      isLiveContext,
      navigate,
      sessionContext,
      setActiveSessionId,
      setActiveSessionTitle,
      setSessionSearchQuery,
    ]
  );

  // Handle rename with API call (optimistic update with revert on failure)
  const handleSessionRename = useCallback(
    async (session: ChatSession, newTitle: string) => {
      const capturedSession = { ...session };
      const capturedContext = sessionContext;
      const invocationLease = invocationLeaseRef.current;
      // A retained A callback must not change B's token before its lease is
      // proven live.
      if (!isLiveContext(capturedContext)) return;
      if (pendingRenameRef.current.has(capturedSession.id)) return;
      const admission = Symbol("rename");
      pendingRenameRef.current.set(capturedSession.id, admission);
      const token = nextRowToken(capturedSession.id);
      const isCurrent = () =>
        isLiveContext(capturedContext) &&
        invocationLease?.active === true &&
        invocationLeaseRef.current === invocationLease &&
        rowTokenRef.current.get(capturedSession.id) === token;
      const originalTitle = capturedSession.title;
      if (!isCurrent()) return;

      // Optimistic update: update local state immediately
      setSessions((prev) =>
        isCurrent() ? prev.map((s) =>
          s.id === capturedSession.id ? { ...s, title: newTitle } : s
        ) : prev
      );
      if (!isCurrent()) return;

      try {
        await updateChatSession(capturedSession.id, newTitle);
      } catch (err) {
        if (!isCurrent()) return;
        setSessions((prev) =>
          isCurrent() ? prev.map((s) =>
            s.id === capturedSession.id ? { ...s, title: originalTitle } : s
          ) : prev
        );
        if (!isCurrent()) return;
        const message = err instanceof Error ? err.message : "Failed to rename session";
        console.warn("Rename failed, reverted:", message);
        toast.error("Failed to rename session. Reverted to original title.");
      } finally {
        if (pendingRenameRef.current.get(capturedSession.id) === admission) {
          pendingRenameRef.current.delete(capturedSession.id);
        }
      }
    },
    [isLiveContext, nextRowToken, rowTokenRef, sessionContext]
  );

  // Handle delete with optimistic UI removal + 5-second undo window.
  // The actual API call is delayed until the undo window expires. If the user
  // clicks Undo, the timer is cancelled and the session is restored — the
  // backend is never called, so there is nothing to restore server-side.
  const handleSessionDelete = useCallback(
    (session: ChatSession) => {
      const capturedSession = { ...session };
      const capturedContext = sessionContext;
      const invocationLease = invocationLeaseRef.current;
      // A retained A callback must not change B's token before its lease is
      // proven live.
      if (!isLiveContext(capturedContext)) return;
      const priorPending = pendingDeletesRef.current.get(capturedSession.id);
      if (priorPending) {
        if (sameSessionContext(priorPending.context, capturedContext)) return;
        clearTimeout(priorPending.timer);
        releaseDeleteRecord(priorPending);
      }
      const token = nextRowToken(capturedSession.id);
      const isOperationCurrent = () =>
        sameSessionContext(capturedContext, latestSessionContextRef.current) &&
        isCurrentAuthOwner(capturedContext.owner) &&
        captureAuthPrincipalGeneration() === capturedContext.principalGeneration &&
        rowTokenRef.current.get(capturedSession.id) === token;
      const isCurrent = () =>
        isOperationCurrent() &&
        isLiveContext(capturedContext) &&
        invocationLease?.active === true &&
        invocationLeaseRef.current === invocationLease;
      if (!isCurrent()) return;

      // Optimistic removal
      setSessions((prev) => isCurrent() ? prev.filter((s) => s.id !== capturedSession.id) : prev);
      if (!isCurrent()) return;
      const wasActive = String(capturedSession.id) === activeSessionId;
      const wasPinned = isSessionPinned(capturedSession.id);
      if (wasActive) {
        setActiveSessionId(null);
        if (!isCurrent()) return;
        setActiveSessionTitle(null);
        if (!isCurrent()) return;
        // Stop the chat store from targeting the deleted session (UI-044) -
        // otherwise the transcript keeps writing into a session slated for
        // deletion until the undo window expires.
        useChatStore.getState().newChat();
        if (!isCurrent()) return;
        navigate("/chat");
        if (!isCurrent()) return;
      }

      const timer = setTimeout(async () => {
        const current = pendingDeletesRef.current.get(capturedSession.id);
        if (
          current !== pending ||
          pending.undone ||
          pending.finalized ||
          !isOperationCurrent()
        ) {
          if (current === pending) releaseDeleteRecord(pending);
          return;
        }
        pending.finalized = true;
        try {
          await deleteChatSession(capturedSession.id);
          if (!isOperationCurrent()) return;
          if (
            _sessionCache.owner === capturedContext.owner &&
            _sessionCache.principalGeneration === capturedContext.principalGeneration &&
            _sessionCache.vaultId === capturedContext.vaultId
          ) {
            _sessionCache.ts = 0;
          }
          if (wasPinned && isSessionPinned(capturedSession.id)) {
            togglePinSession(capturedSession.id);
          }
        } catch (err) {
          const isPendingDeleteCurrent = () =>
            pendingDeletesRef.current.get(capturedSession.id) === pending &&
            pending.finalized &&
            !pending.undone &&
            rowTokenRef.current.get(capturedSession.id) === pending.token &&
            sameSessionContext(pending.context, latestSessionContextRef.current) &&
            isCurrentAuthOwner(pending.context.owner) &&
            captureAuthPrincipalGeneration() === pending.context.principalGeneration;
          if (!isPendingDeleteCurrent()) return;

          // The logical delete may outlive this rail, but row restoration stays local.
          const restoreLocalFailure = () => {
            if (!isCurrent()) return false;
            setSessions((prev) => {
              if (!isCurrent() || prev.some((item) => item.id === capturedSession.id)) return prev;
              return [...prev, capturedSession].sort(
                (a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime()
              );
            });
            if (!isCurrent()) return false;
            if (wasActive && useChatShellStore.getState().activeSessionId === null) {
              setActiveSessionId(String(capturedSession.id));
              if (!isCurrent()) return false;
              setActiveSessionTitle(capturedSession.title || null);
              if (!isCurrent()) return false;
              navigate(`/chat/${capturedSession.id}`);
              if (!isCurrent()) return false;
            }
            return isCurrent();
          };
          const restoredWhileMounted = restoreLocalFailure();
          if (!isPendingDeleteCurrent()) return;
          const msg = err instanceof Error ? err.message : "Failed to delete session";
          toast.error(
            restoredWhileMounted
              ? "Could not delete session. It has been restored."
              : "Could not delete session.",
            { description: msg },
          );
        } finally {
          releaseDeleteRecord(pending);
        }
      }, 5000);

      const pending: PendingDelete = {
        token,
        session: capturedSession,
        context: capturedContext,
        wasActive,
        wasPinned,
        timer,
        undone: false,
        finalized: false,
      };
      pendingDeletesRef.current.set(capturedSession.id, pending);

      toast(`"${capturedSession.title || "Untitled"}" deleted`, {
        action: {
          label: "Undo",
          onClick: () => {
            const current = pendingDeletesRef.current.get(capturedSession.id);
            if (current !== pending || pending.undone || pending.finalized || !isOperationCurrent()) return;
            pending.undone = true;
            clearTimeout(pending.timer);
            releaseDeleteRecord(pending);
            if (!isCurrent()) return;
            // Restore session in UI
            setSessions((prev) => {
              if (!isCurrent() || prev.some((item) => item.id === capturedSession.id)) return prev;
              return [...prev, capturedSession].sort(
                (a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime()
              );
            });
            if (!isCurrent()) return;
            if (wasActive && useChatShellStore.getState().activeSessionId === null) {
              setActiveSessionId(String(capturedSession.id));
              if (!isCurrent()) return;
              setActiveSessionTitle(capturedSession.title || null);
              if (!isCurrent()) return;
              navigate(`/chat/${capturedSession.id}`);
            }
          },
        },
        duration: 5000,
      });
    },
    [
      activeSessionId,
      isLiveContext,
      isSessionPinned,
      latestSessionContextRef,
      navigate,
      nextRowToken,
      pendingDeletesRef,
      releaseDeleteRecord,
      rowTokenRef,
      sessionContext,
      setActiveSessionId,
      setActiveSessionTitle,
      togglePinSession,
    ]
  );

  // Retry loading sessions
  const handleRetry = useCallback(async () => {
    const readContext = sessionContext;
    if (!isLiveContext(readContext)) return;
    const invocationLease = invocationLeaseRef.current;
    if (!invocationLease?.active) return;
    const readAttempt = ++sessionReadAttemptRef.current;
    const isCurrent = () =>
      isLiveContext(readContext) && invocationLease.active && invocationLeaseRef.current === invocationLease &&
      readAttempt === sessionReadAttemptRef.current;
    if (
      (_sessionCache.owner === readContext.owner &&
        _sessionCache.principalGeneration === readContext.principalGeneration &&
        _sessionCache.vaultId === readContext.vaultId)
    ) {
      _sessionCache.ts = 0;
    }
    if (!isCurrent()) return;
    setIsLoading((previous) => isCurrent() ? true : previous);
    setError((previous) => isCurrent() ? null : previous);
    try {
      if (!isCurrent()) return;
      const cacheReadAttempt = (_sessionCache.readAttempt ?? 0) + 1;
      _sessionCache.readAttempt = cacheReadAttempt;
      if (!isCurrent()) return;
      const data = await listChatSessions(vaultId);
      if (!data || !Array.isArray(data.sessions)) {
        throw new Error("Session list response was malformed");
      }
      if (!isCurrent()) return;
      if (_sessionCache.readAttempt === cacheReadAttempt) {
        _sessionCache.data = data.sessions;
        _sessionCache.vaultId = vaultId;
        _sessionCache.owner = readContext.owner;
        _sessionCache.principalGeneration = readContext.principalGeneration;
        _sessionCache.ts = Date.now();
      }
      setSessions((previous) => isCurrent() ? data.sessions : previous);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to load sessions";
      if (isCurrent()) setError((previous) => isCurrent() ? message : previous);
    } finally {
      if (isCurrent()) setIsLoading((previous) => isCurrent() ? false : previous);
    }
  }, [isLiveContext, sessionContext, vaultId]);

  // Loading skeleton
  if (isLoading) {
    return (
      <>
        {paletteAction}
        <div className={`flex h-full flex-col ${className || ""}`}>
          <div className="flex items-center justify-between mb-4">
            <Skeleton className="h-4 w-20" />
            <Skeleton className="h-8 w-24" />
          </div>
          <Skeleton className="h-9 w-full mb-4" />
          <div className="space-y-4 flex-1">
            {[...Array(5)].map((_, i) => (
              <div key={i} className="flex items-center gap-3 px-2 h-16">
                <Skeleton className="h-4 w-4 shrink-0" />
                <div className="flex-1 space-y-2">
                  <Skeleton className="h-4 w-[150px]" />
                  <Skeleton className="h-3 w-[100px]" />
                </div>
              </div>
            ))}
          </div>
        </div>
      </>
    );
  }

  // Error state
  if (error) {
    return (
      <>
        {paletteAction}
        <div className={`flex h-full flex-col ${className || ""}`}>
          <div className="flex flex-col items-center justify-center py-8 text-center flex-1">
            <AlertCircle className="w-10 h-10 mb-3 text-destructive" aria-hidden="true" />
            <p className="text-sm text-muted-foreground">Failed to load sessions</p>
            <p className="text-xs text-muted-foreground mt-1">{error}</p>
            <p className="text-xs text-muted-foreground mt-2 max-w-[200px]">
              Check your network connection, then retry. If this persists, the
              chat service may be temporarily unavailable.
            </p>
            <Button variant="outline" size="sm" onClick={handleRetry} className="mt-3">
              Retry
            </Button>
          </div>
        </div>
      </>
    );
  }

  // Empty state (no sessions at all). Branch on whether the active vault has
  // any indexed documents — if not, the more useful next step is uploading
  // documents, not opening another empty chat.
  if (sessions.length === 0) {
    const hasIndexedDocs = activeVault ? activeVault.file_count > 0 : false;
    return (
      <>
        {paletteAction}
        <div className={`flex h-full flex-col ${className || ""}`}>
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-sm font-semibold text-muted-foreground uppercase tracking-wide">
              Sessions
            </h2>
            <Button size="sm" onClick={handleNewChat}>
              <Plus className="mr-1.5 h-4 w-4" aria-hidden="true" />
              New Chat
            </Button>
          </div>
          <div className="flex flex-col items-center justify-center py-12 text-center flex-1 px-3">
            <MessageSquare className="w-12 h-12 text-muted-foreground mb-4" aria-hidden="true" />
            <p className="text-sm text-muted-foreground">No sessions yet</p>
            {hasIndexedDocs ? (
              <>
                <p className="text-xs text-muted-foreground mt-1">
                  Start a new chat to begin a conversation
                </p>
                <Button variant="outline" className="mt-4" onClick={handleNewChat}>
                  <Plus className="mr-1.5 h-4 w-4" aria-hidden="true" />
                  New Chat
                </Button>
              </>
            ) : (
              <>
                <p className="text-xs text-muted-foreground mt-1 max-w-[220px]">
                  Upload documents first so the assistant has something to ground
                  its answers in.
                </p>
                <Button
                  variant="outline"
                  className="mt-4"
                  onClick={() => navigate("/documents")}
                >
                  Go to Documents
                </Button>
                <button
                  onClick={handleNewChat}
                  className="mt-3 text-xs text-muted-foreground underline hover:text-muted-foreground"
                >
                  Start a new chat anyway
                </button>
              </>
            )}
          </div>
        </div>
      </>
    );
  }

  // Empty search results
  const hasSearchResults = Object.values(groupedSessions).some(
    (group) => group.length > 0
  );

  return (
    <>
      {paletteAction}
      <div className={`flex h-full flex-col ${className || ""} p-3 px-6 bg-card/80`}>
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-sm font-semibold text-muted-foreground uppercase tracking-wide">
          Sessions
        </h2>
        <div className="flex items-center gap-1">
          <Button
            variant="outline"
            size="sm"
            onClick={() => dispatchCommandPaletteOpen()}
            aria-label="Open command palette"
          >
            <Search className="mr-1.5 h-4 w-4" aria-hidden="true" />
            Commands
          </Button>
          <Button size="sm" onClick={handleNewChat} aria-label="Start new chat">
            <Plus className="mr-1.5 h-4 w-4" aria-hidden="true" />
            New Chat
          </Button>
        </div>
      </div>

      {/* Search */}
      <ChatSearchInput
        value={sessionSearchQuery}
        onChange={setSessionSearchQuery}
        className="mb-4"
      />

      {/* Sessions List */}
      <div ref={listRef} className="flex-1 overflow-y-auto scrollbar-thin scrollbar-thumb-muted-foreground/20 scrollbar-track-transparent">
        {!hasSearchResults ? (
          <div className="flex flex-col items-center justify-center py-8 text-center">
            <Search className="w-10 h-10 text-muted-foreground mb-3" aria-hidden="true" />
            <p className="text-sm text-muted-foreground">No sessions found</p>
            <p className="text-xs text-muted-foreground mt-1">
              Try adjusting your search
            </p>
            <Button
              variant="ghost"
              size="sm"
              className="mt-2"
              onClick={() => setSessionSearchQuery("")}
            >
              Clear search
            </Button>
          </div>
        ) : (() => {
          const virtualItems = sessionVirtualizer.getVirtualItems();
          const shouldUseVirtualizer = virtualItems.length > 0;
          return shouldUseVirtualizer ? (
            // eslint-disable-next-line jsx-a11y-x/no-static-element-interactions -- container uses event delegation for roving-tabindex arrow-key navigation (UI-045); rows carry data-session-index and receive programmatic focus. APG listbox pattern.
            <div
              style={{ height: sessionVirtualizer.getTotalSize(), position: 'relative' }}
              onKeyDown={handleSessionListKeyDown}
            >
              {virtualItems.map((virtualItem) => {
                const session = filteredSessions[virtualItem.index];
                return (
                  // eslint-disable-next-line jsx-a11y-x/no-static-element-interactions -- focusable row wrapper (SessionItem forwards no unknown props to its root); Enter/Space activate the session when the wrapper holds focus.
                  <div
                    key={virtualItem.key}
                    data-index={virtualItem.index}
                    data-session-index={virtualItem.index}
                    tabIndex={-1}
                    ref={sessionVirtualizer.measureElement}
                    style={{ position: 'absolute', top: virtualItem.start, left: 0, right: 0 }}
                    onKeyDown={(e) => {
                      // Activation for wrapper-focused rows; when focus sits on
                      // SessionItem's own root it handles Enter/Space itself.
                      if (e.target !== e.currentTarget) return;
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        if (handleSessionClick(session)) {
                          setFocusedSessionIndex(virtualItem.index);
                        }
                      }
                    }}
                  >
                    <SessionItem
                      session={session}
                      isActive={String(session.id) === activeSessionId}
                      isPinned={isSessionPinned(session.id)}
                      onClick={() => {
                        if (handleSessionClick(session)) {
                          setFocusedSessionIndex(virtualItem.index);
                        }
                      }}
                      onRename={(newTitle) => handleSessionRename(session, newTitle)}
                      onPinToggle={() => togglePinSession(session.id)}
                      onDelete={() => handleSessionDelete(session)}
                      tabIndex={virtualItem.index === focusedSessionIndex ? 0 : -1}
                    />
                  </div>
                );
              })}
            </div>
          ) : (
            // Fallback: render all items directly (used in JSDOM/test env where
            // scroll container has 0 height and getVirtualItems() returns [])
            // eslint-disable-next-line jsx-a11y-x/no-static-element-interactions -- container uses event delegation for roving-tabindex arrow-key navigation (UI-045). APG listbox pattern.
            <div onKeyDown={handleSessionListKeyDown}>
              {filteredSessions.map((session, index) => (
                // eslint-disable-next-line jsx-a11y-x/no-static-element-interactions -- focusable row wrapper (SessionItem forwards no unknown props to its root); Enter/Space activate the session when the wrapper holds focus.
                <div
                  key={session.id}
                  data-session-index={index}
                  tabIndex={-1}
                  onKeyDown={(e) => {
                    if (e.target !== e.currentTarget) return;
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      if (handleSessionClick(session)) {
                        setFocusedSessionIndex(index);
                      }
                    }
                  }}
                >
                  <SessionItem
                    session={session}
                    isActive={String(session.id) === activeSessionId}
                    isPinned={isSessionPinned(session.id)}
                    onClick={() => {
                      if (handleSessionClick(session)) {
                        setFocusedSessionIndex(index);
                      }
                    }}
                    onRename={(newTitle) => handleSessionRename(session, newTitle)}
                    onPinToggle={() => togglePinSession(session.id)}
                    onDelete={() => handleSessionDelete(session)}
                    tabIndex={index === focusedSessionIndex ? 0 : -1}
                  />
                </div>
              ))}
            </div>
          );
        })()}
      </div>
      </div>
    </>
  );
}

export default SessionRail;
