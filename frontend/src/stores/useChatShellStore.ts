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

const isMobile = () => {
  if (typeof window === "undefined") return false;
  return window.innerWidth < 768;
};

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

// Persist pinned sessions to localStorage
const persistPinnedSessions = (ids: number[]) => {
  if (typeof window === "undefined") return;
  try {
    localStorage.setItem(PINNED_SESSIONS_KEY, JSON.stringify(ids));
  } catch {
    // Silently fail on localStorage errors (quota exceeded, etc.)
  }
};

export const useChatShellStore = create<ChatShellState>((set, get) => ({
  sessionRailOpen: !isMobile(),
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
    persistPinnedSessions(newIds);
    set({ pinnedSessionIds: newIds });
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

// Issue #685 (T1-13-S2-11): keep this tab's pinned list coherent with edits
// from other tabs. The storage event fires only for cross-document changes,
// and the persisted value is authoritative — REPLACE from it (a union would
// resurrect pins another tab just removed). key === null (a cross-tab clear)
// is left to the next toggle's fresh read.
if (typeof window !== "undefined") {
  window.addEventListener("storage", (event) => {
    if (event.key !== PINNED_SESSIONS_KEY) return;
    if (event.newValue === null) return;
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
