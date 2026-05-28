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
    const { pinnedSessionIds } = get();
    const isPinned = pinnedSessionIds.includes(sessionId);
    const newIds = isPinned
      ? pinnedSessionIds.filter((id) => id !== sessionId)
      : [...pinnedSessionIds, sessionId];
    persistPinnedSessions(newIds);
    set({ pinnedSessionIds: newIds });
  },
  isSessionPinned: (sessionId) => {
    return get().pinnedSessionIds.includes(sessionId);
  },
  // Evidence pane actions
  setSelectedEvidenceSource: (source) => set({ selectedEvidenceSource: source }),
  setActiveRightTab: (tab) => set({ activeRightTab: tab }),
}));
