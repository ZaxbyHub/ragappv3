import { create } from "zustand";
import type { Source } from "@/lib/api";

const PINNED_SESSIONS_KEY = "ragapp_pinned_sessions";

export type RightPaneTab = "evidence" | "preview" | "wiki";

interface ChatShellState {
  sessionRailOpen: boolean;
  rightPaneOpen: boolean;
  rightPaneWidth: number;
  sessionRailWidth: number;
  activeSessionId: string | null;
  activeSessionTitle: string | null;
  sessionListRefreshToken: number;
  sessionSearchQuery: string;
  pinnedSessionIds: number[];
  // Evidence pane state
  selectedEvidenceSource: Source | null;
  /** Message the current evidence selection originated from (jump anchor). */
  selectedEvidenceMessageId: string | null;
  /** Message whose citation chip should regain focus when the pane closes. */
  evidenceReturnFocusId: string | null;
  activeRightTab: RightPaneTab;
  toggleSessionRail: () => void;
  toggleRightPane: () => void;
  setRightPaneWidth: (width: number) => void;
  setSessionRailWidth: (width: number) => void;
  setActiveSessionId: (id: string | null) => void;
  setActiveSessionTitle: (title: string | null) => void;
  requestSessionListRefresh: () => void;
  openSessionRail: () => void;
  closeSessionRail: () => void;
  openRightPane: () => void;
  closeRightPane: () => void;
  setSessionSearchQuery: (query: string) => void;
  togglePinSession: (sessionId: number) => void;
  isSessionPinned: (sessionId: number) => boolean;
  // Evidence pane actions
  setSelectedEvidenceSource: (source: Source | null) => void;
  setSelectedEvidenceMessageId: (id: string | null) => void;
  setEvidenceReturnFocusId: (id: string | null) => void;
  /** Clear the whole evidence selection (source, jump anchor, focus target). */
  resetEvidenceSelection: () => void;
  setActiveRightTab: (tab: RightPaneTab) => void;
}

const DEFAULT_RIGHT_PANE_WIDTH = 400;
const MIN_RIGHT_PANE_WIDTH = 320;
const MAX_RIGHT_PANE_WIDTH = 600;

const MIN_SESSION_RAIL_WIDTH = 240;
const MAX_SESSION_RAIL_WIDTH = 400;
const DEFAULT_SESSION_RAIL_WIDTH = 320;

// Load pinned sessions from localStorage
const loadPinnedSessions = (): number[] => {
  if (typeof window === "undefined") return [];
  try {
    const stored = localStorage.getItem(PINNED_SESSIONS_KEY);
    if (stored) {
      const parsed = JSON.parse(stored);
      if (Array.isArray(parsed)) {
        return parsed.filter((id): id is number => typeof id === "number");
      }
    }
  } catch {
    // Fallback to empty array on parse error
  }
  return [];
};

// Persist pinned sessions to localStorage. Returns whether the write landed —
// callers keep the in-memory list coherent with storage on failure (F-005).
const persistPinnedSessions = (ids: number[]): boolean => {
  if (typeof window === "undefined") return false;
  try {
    localStorage.setItem(PINNED_SESSIONS_KEY, JSON.stringify(ids));
    return true;
  } catch {
    // Quota/security failure: report so state can revert to storage truth.
    return false;
  }
};

// The session rail defaults open only at >=lg (1024px) — the same breakpoint
// ChatShell's useIsMobile(1024) uses for the below-lg evidence sheet. Between
// md and lg the expanded nav rail (240px) plus a 320px session rail leaves
// the chat column ~208px (issue #689 / UI-R1-03), so below lg the rail
// starts CLOSED by default; the user can still open it from its toggle at
// any width (reopening it at 768px narrows the column again — user choice).
// SSR/jsdom (innerWidth 1024) sees the same open default as before.
const isDesktopViewport = () => {
  if (typeof window === "undefined") return true;
  return window.innerWidth >= 1024;
};

export const useChatShellStore = create<ChatShellState>((set, get) => ({
  sessionRailOpen: isDesktopViewport(),
  rightPaneOpen: false,
  rightPaneWidth: DEFAULT_RIGHT_PANE_WIDTH,
  sessionRailWidth: DEFAULT_SESSION_RAIL_WIDTH,
  activeSessionId: null,
  activeSessionTitle: null,
  sessionListRefreshToken: 0,
  sessionSearchQuery: "",
  pinnedSessionIds: loadPinnedSessions(),
  // Evidence pane state
  selectedEvidenceSource: null,
  selectedEvidenceMessageId: null,
  evidenceReturnFocusId: null,
  activeRightTab: "evidence",
  toggleSessionRail: () => set((state) => ({ sessionRailOpen: !state.sessionRailOpen })),
  toggleRightPane: () => set((state) => ({ rightPaneOpen: !state.rightPaneOpen })),
  setRightPaneWidth: (width) => set({ rightPaneWidth: Math.max(MIN_RIGHT_PANE_WIDTH, Math.min(MAX_RIGHT_PANE_WIDTH, width)) }),
  setSessionRailWidth: (width) => set({ sessionRailWidth: Math.max(MIN_SESSION_RAIL_WIDTH, Math.min(MAX_SESSION_RAIL_WIDTH, width)) }),
  setActiveSessionId: (id) => set({ activeSessionId: id }),
  setActiveSessionTitle: (title) => set({ activeSessionTitle: title }),
  requestSessionListRefresh: () =>
    set((state) => ({ sessionListRefreshToken: state.sessionListRefreshToken + 1 })),
  openSessionRail: () => set({ sessionRailOpen: true }),
  closeSessionRail: () => set({ sessionRailOpen: false }),
  openRightPane: () => set({ rightPaneOpen: true }),
  closeRightPane: () => set({ rightPaneOpen: false }),
  setSessionSearchQuery: (query) => set({ sessionSearchQuery: query }),
  togglePinSession: (sessionId) => {
    // Issue #685 (T1-13-S2-11): re-read storage instead of trusting the
    // in-memory list — another tab may have pinned/unpinned sessions since
    // this tab loaded, and rewriting from memory would silently drop them.
    const current = loadPinnedSessions();
    const isPinned = current.includes(sessionId);
    const newIds = isPinned
      ? current.filter((id) => id !== sessionId)
      : [...current, sessionId];
    // F-005: if the write fails (quota exhausted), keep memory equal to
    // storage — a phantom in-memory pin could otherwise never be unpinned,
    // because the next toggle would re-read storage and compute "pin" again.
    if (persistPinnedSessions(newIds)) {
      set({ pinnedSessionIds: newIds });
    } else {
      set({ pinnedSessionIds: current });
    }
  },
  isSessionPinned: (sessionId) => {
    return get().pinnedSessionIds.includes(sessionId);
  },
  // Evidence pane actions
  setSelectedEvidenceSource: (source) => set({ selectedEvidenceSource: source }),
  setSelectedEvidenceMessageId: (id) => set({ selectedEvidenceMessageId: id }),
  setEvidenceReturnFocusId: (id) => set({ evidenceReturnFocusId: id }),
  resetEvidenceSelection: () =>
    set({
      selectedEvidenceSource: null,
      selectedEvidenceMessageId: null,
      evidenceReturnFocusId: null,
    }),
  setActiveRightTab: (tab) => set({ activeRightTab: tab }),
}));

// Issue #685 (external review F-007 / review PRR-007): keep this tab's pinned
// list coherent with edits from other tabs. The storage event fires only for
// cross-document changes, and the persisted value is authoritative — REPLACE
// from it (a union would resurrect pins another tab just removed). A removal
// or clear of the key (newValue === null, or a cross-tab clear() surfacing as
// key === null) means the pins are gone: mirror that instead of leaving stale
// in-memory pins that a later toggle would resurrect.
if (typeof window !== "undefined") {
  window.addEventListener("storage", (event) => {
    if (event.key !== null && event.key !== PINNED_SESSIONS_KEY) return;
    if (event.newValue === null) {
      useChatShellStore.setState({ pinnedSessionIds: [] });
      return;
    }
    let next: number[] = [];
    try {
      const parsed = JSON.parse(event.newValue);
      if (Array.isArray(parsed)) {
        next = parsed.filter((id): id is number => typeof id === "number");
      }
    } catch {
      // Unparseable external write: fall back to empty rather than keeping a
      // list that no longer matches storage.
    }
    useChatShellStore.setState({ pinnedSessionIds: next });
  });
}
