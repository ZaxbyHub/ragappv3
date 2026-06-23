// frontend/src/components/chat/SessionRail.tsx
// SessionRail component with full business logic for chat session management

import { useState, useCallback, useMemo, useEffect, useRef, useLayoutEffect, forwardRef } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useDebounce } from "@/hooks/useDebounce";
import { useNavigate } from "react-router-dom";
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

export function ChatSearchInput({
  value,
  onChange,
  placeholder = "Search sessions...",
  className,
}: ChatSearchInputProps) {
  const inputRef = useRef<HTMLInputElement>(null);

  // Ctrl+K keyboard shortcut to focus search
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "k") {
        e.preventDefault();
        inputRef.current?.focus();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
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
              className="h-5 w-5"
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
            <HugeiconsIcon icon={Message01Icon} strokeWidth={1.2} size={16} color={isActive ? "text-accent-foreground" : "text-muted-foreground"} aria-hidden="true" />

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
                    className="h-5 w-5"
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
                    className="h-5 w-5"
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
                    <span title="Branched conversation"><GitBranch className="h-3 w-3 text-muted-foreground flex-shrink-0" aria-hidden="true" /></span>
                  )}
                  {isPinned && (
                    <Pin className="h-3 w-3 text-muted-foreground flex-shrink-0" aria-hidden="true" />
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
                          className="text-destructive focus:text-destructive bg-destructive/10 hover:!bg-destructive/20"
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
export const _sessionCache: { data: ChatSession[] | null; vaultId?: number; ts: number } = {
  data: null,
  ts: 0,
};
const SESSION_CACHE_TTL = 5000; // 5 seconds

interface SessionRailProps {
  vaultId?: number;
  className?: string;
}

export function SessionRail({ vaultId, className }: SessionRailProps) {
  const testMode = useTestMode();
  const navigate = useNavigate();
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
  // Tracks pending (delayed) delete timers so Undo can cancel them
  const pendingDeletesRef = useRef<Map<number, ReturnType<typeof setTimeout>>>(new Map());

  // H-7 fix: Debounce search to avoid firing API calls per keystroke
  const [debouncedSearchQuery] = useDebounce(sessionSearchQuery, 300);

  // Fetch sessions on mount and when vaultId changes (with dedup cache)
  const lastRefreshTokenRef = useRef(sessionListRefreshToken);
  useEffect(() => {
    let cancelled = false;
    const forceRefresh = sessionListRefreshToken !== lastRefreshTokenRef.current;
    lastRefreshTokenRef.current = sessionListRefreshToken;
    const fetchSessions = async () => {
      if (testMode) {
        setSessions(mockChatSessions);
        setIsLoading(false);
        return;
      }
      // Use cache if fresh and same vault
      if (
        !forceRefresh &&
        _sessionCache.data &&
        _sessionCache.vaultId === vaultId &&
        Date.now() - _sessionCache.ts < SESSION_CACHE_TTL
      ) {
        setSessions(_sessionCache.data);
        setIsLoading(false);
        return;
      }

      setIsLoading(true);
      setError(null);
      try {
        const data = await listChatSessions(vaultId);
        const sessionList = Array.isArray(data.sessions) ? data.sessions : [];
        _sessionCache.data = sessionList;
        _sessionCache.vaultId = vaultId;
        _sessionCache.ts = Date.now();
        if (!cancelled) setSessions(sessionList);
      } catch (err) {
        const message = err instanceof Error ? err.message : "Failed to load sessions";
        if (!cancelled) setError(message);
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    };

    fetchSessions();
    return () => { cancelled = true; };
  }, [vaultId, sessionListRefreshToken, testMode]);

  // Track which session IDs have been fetched to avoid duplicate fetches
  const fetchedIdsRef = useRef<Set<number>>(new Set());

  // Fetch session details for first message content when needed for search
  useEffect(() => {
    const fetchSessionDetails = async () => {
      if (!debouncedSearchQuery.trim()) {
        setSessionDetails(new Map());
        return;
      }

      // Only fetch details for sessions we don't already have
      const sessionsNeedingDetails = sessions.filter((s) => !fetchedIdsRef.current.has(s.id));

      if (sessionsNeedingDetails.length === 0) return;

      const newDetails = new Map(sessionDetails);

      await Promise.all(
        sessionsNeedingDetails.slice(0, 50).map(async (session) => {
          try {
            const detail = await getChatSession(session.id);
            newDetails.set(session.id, detail);
            fetchedIdsRef.current.add(session.id);
          } catch {
            // Silently fail for individual session fetch errors
          }
        })
      );

      setSessionDetails(newDetails);
    };

    fetchSessionDetails();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debouncedSearchQuery, sessions]);

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

  // H-1: Virtualizer refs and setup
  const listRef = useRef<HTMLDivElement>(null);
  const sessionVirtualizer = useVirtualizer({
    count: filteredSessions.length,
    getScrollElement: () => listRef.current,
    estimateSize: () => 64,
    measureElement: (el) => el?.getBoundingClientRect().height ?? 64,
    overscan: 5,
  });

  // Handle new chat
  const handleNewChat = useCallback(() => {
    useChatStore.getState().newChat();
    setActiveSessionId(null);
    setActiveSessionTitle(null);
    setSessionSearchQuery("");
    navigate("/chat");
  }, [navigate, setActiveSessionId, setActiveSessionTitle, setSessionSearchQuery]);

  // Handle session click
  const handleSessionClick = useCallback(
    (session: ChatSession) => {
      setActiveSessionId(String(session.id));
      setActiveSessionTitle(session.title || null);
      setSessionSearchQuery("");
      navigate(`/chat/${session.id}`);
    },
    [navigate, setActiveSessionId, setActiveSessionTitle, setSessionSearchQuery]
  );

  // Handle rename with API call (optimistic update with revert on failure)
  const handleSessionRename = useCallback(
    async (_session: ChatSession, newTitle: string) => {
      const originalTitle = _session.title;
      
      // Optimistic update: update local state immediately
      setSessions((prev) =>
        prev.map((s) =>
          s.id === _session.id ? { ...s, title: newTitle } : s
        )
      );
      
      try {
        await updateChatSession(_session.id, newTitle);
      } catch (err) {
        setSessions((prev) =>
          prev.map((s) =>
            s.id === _session.id ? { ...s, title: originalTitle } : s
          )
        );
        const message = err instanceof Error ? err.message : "Failed to rename session";
        console.warn("Rename failed, reverted:", message);
        toast.error("Failed to rename session. Reverted to original title.");
      }
    },
    []
  );

  // Handle delete with optimistic UI removal + 5-second undo window.
  // The actual API call is delayed until the undo window expires. If the user
  // clicks Undo, the timer is cancelled and the session is restored — the
  // backend is never called, so there is nothing to restore server-side.
  const handleSessionDelete = useCallback(
    (session: ChatSession) => {
      // Optimistic removal
      setSessions((prev) => prev.filter((s) => s.id !== session.id));
      const wasActive = String(session.id) === activeSessionId;
      if (wasActive) {
        setActiveSessionId(null);
        navigate("/chat");
      }

      const timer = setTimeout(async () => {
        pendingDeletesRef.current.delete(session.id);
        try {
          await deleteChatSession(session.id);
          _sessionCache.ts = 0;
          if (isSessionPinned(session.id)) togglePinSession(session.id);
        } catch (err) {
          // Hard delete failed — restore session in UI and show error
          setSessions((prev) =>
            [...prev, session].sort(
              (a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime()
            )
          );
          const msg = err instanceof Error ? err.message : "Failed to delete session";
          toast.error("Could not delete session. It has been restored.", { description: msg });
        }
      }, 5000);

      pendingDeletesRef.current.set(session.id, timer);

      toast(`"${session.title || "Untitled"}" deleted`, {
        action: {
          label: "Undo",
          onClick: () => {
            const t = pendingDeletesRef.current.get(session.id);
            if (t !== undefined) {
              clearTimeout(t);
              pendingDeletesRef.current.delete(session.id);
            }
            // Restore session in UI
            setSessions((prev) =>
              [...prev, session].sort(
                (a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime()
              )
            );
            if (wasActive) {
              setActiveSessionId(String(session.id));
              navigate(`/chat/${session.id}`);
            }
          },
        },
        duration: 5000,
      });
    },
    [activeSessionId, isSessionPinned, navigate, setActiveSessionId, togglePinSession]
  );

  // Retry loading sessions
  const handleRetry = useCallback(async () => {
    _sessionCache.ts = 0; // Invalidate cache for retry
    setIsLoading(true);
    setError(null);
    try {
      const data = await listChatSessions(vaultId);
      setSessions(data.sessions);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to load sessions";
      setError(message);
    } finally {
      setIsLoading(false);
    }
  }, [vaultId]);

  // Loading skeleton
  if (isLoading) {
    return (
      <div className={`flex h-full flex-col ${className || ""}`}>
        <div className="flex items-center justify-between mb-4">
          <Skeleton className="h-4 w-20" />
          <Skeleton className="h-8 w-24" />
        </div>
        <Skeleton className="h-9 w-full mb-4" />
        <div className="space-y-4 flex-1">
          {[...Array(5)].map((_, i) => (
            <div key={i} className="flex items-center gap-3 px-2 h-16">
              <Skeleton className="h-4 w-4 flex-shrink-0" />
              <div className="flex-1 space-y-2">
                <Skeleton className="h-4 w-[150px]" />
                <Skeleton className="h-3 w-[100px]" />
              </div>
            </div>
          ))}
        </div>
      </div>
    );
  }

  // Error state
  if (error) {
    return (
      <div className={`flex h-full flex-col ${className || ""}`}>
        <div className="flex flex-col items-center justify-center py-8 text-center flex-1">
          <AlertCircle className="w-10 h-10 mb-3 text-destructive" aria-hidden="true" />
          <p className="text-sm text-muted-foreground">Failed to load sessions</p>
          <p className="text-xs text-muted-foreground/70 mt-1">{error}</p>
          <p className="text-xs text-muted-foreground/60 mt-2 max-w-[200px]">
            Check your network connection, then retry. If this persists, the
            chat service may be temporarily unavailable.
          </p>
          <Button variant="outline" size="sm" onClick={handleRetry} className="mt-3">
            Retry
          </Button>
        </div>
      </div>
    );
  }

  // Empty state (no sessions at all). Branch on whether the active vault has
  // any indexed documents — if not, the more useful next step is uploading
  // documents, not opening another empty chat.
  if (sessions.length === 0) {
    const hasIndexedDocs = activeVault ? activeVault.file_count > 0 : false;
    return (
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
              <p className="text-xs text-muted-foreground/70 mt-1">
                Start a new chat to begin a conversation
              </p>
              <Button variant="outline" className="mt-4" onClick={handleNewChat}>
                <Plus className="mr-1.5 h-4 w-4" aria-hidden="true" />
                New Chat
              </Button>
            </>
          ) : (
            <>
              <p className="text-xs text-muted-foreground/70 mt-1 max-w-[220px]">
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
                className="mt-3 text-xs text-muted-foreground/70 underline hover:text-muted-foreground"
              >
                Start a new chat anyway
              </button>
            </>
          )}
        </div>
      </div>
    );
  }

  // Empty search results
  const hasSearchResults = Object.values(groupedSessions).some(
    (group) => group.length > 0
  );

  return (
    <div className={`flex h-full flex-col ${className || ""} p-3 px-6 bg-card/80`}>
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-sm font-semibold text-muted-foreground uppercase tracking-wide">
          Sessions
        </h2>
        <Button size="sm" onClick={handleNewChat} aria-label="Start new chat">
          <Plus className="mr-1.5 h-4 w-4" aria-hidden="true" />
          New Chat
        </Button>
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
            <p className="text-xs text-muted-foreground/70 mt-1">
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
            <div style={{ height: sessionVirtualizer.getTotalSize(), position: 'relative' }}>
              {virtualItems.map((virtualItem) => {
                const session = filteredSessions[virtualItem.index];
                return (
                  <div
                    key={virtualItem.key}
                    data-index={virtualItem.index}
                    ref={sessionVirtualizer.measureElement}
                    style={{ position: 'absolute', top: virtualItem.start, left: 0, right: 0 }}
                  >
                    <SessionItem
                      session={session}
                      isActive={String(session.id) === activeSessionId}
                      isPinned={isSessionPinned(session.id)}
                      onClick={() => {
                        handleSessionClick(session);
                        setFocusedSessionIndex(virtualItem.index);
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
            <div>
              {filteredSessions.map((session, index) => (
                <div key={session.id}>
                  <SessionItem
                    session={session}
                    isActive={String(session.id) === activeSessionId}
                    isPinned={isSessionPinned(session.id)}
                    onClick={() => {
                      handleSessionClick(session);
                      setFocusedSessionIndex(index);
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
  );
}

export default SessionRail;
