import { useEffect, useState, useCallback, useRef } from "react";
import { useParams } from "react-router-dom";
import { cn } from "@/lib/utils";
import { getChatSession } from "@/lib/api";
import { mapSessionMessage } from "@/lib/chatMessageMapper";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { useChatMessages, useChatStore, type Message } from "@/stores/useChatStore";
import { useTestMode } from "@/fixtures/TestModeContext";
import { mockChatMessages } from "@/fixtures/chat";
import { SessionRail } from "@/components/chat/SessionRail";
import { TranscriptPane } from "@/components/chat/TranscriptPane";
import { RightPane } from "@/components/chat/RightPane";
import { VaultSelector } from "@/components/vault/VaultSelector";
import { Button } from "@/components/ui/button";
import {
  useKeyboardShortcuts,
  KeyboardShortcutsDialog,
} from "@/components/shared/KeyboardShortcuts";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { ErrorState } from "@/components/shared/ErrorState";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
  SheetClose,
} from "@/components/ui/sheet";
import { PanelLeft, PanelRight, Download, X } from "lucide-react";

function useIsMobile(breakpoint = 768) {
  const [isMobile, setIsMobile] = useState(
    typeof window !== "undefined" ? window.innerWidth < breakpoint : false
  );
  useEffect(() => {
    const mq = window.matchMedia(`(max-width: ${breakpoint - 1}px)`);
    const handler = (e: MediaQueryListEvent) => setIsMobile(e.matches);
    mq.addEventListener("change", handler);
    setIsMobile(mq.matches);
    return () => mq.removeEventListener("change", handler);
  }, [breakpoint]);
  return isMobile;
}

export default function ChatShell() {
  const testMode = useTestMode();
  const { sessionId } = useParams<{ sessionId?: string }>();
  const {
    sessionRailOpen,
    rightPaneOpen,
    rightPaneWidth,
    sessionRailWidth,
    activeSessionId,
    activeSessionTitle,
    toggleSessionRail,
    toggleRightPane,
    setRightPaneWidth,
    setSessionRailWidth,
    setActiveSessionId,
    closeRightPane,
    evidenceReturnFocusId,
  } = useChatShellStore();

  const isMobile = useIsMobile();
  // Gate right-pane bottom Sheets on sub-lg viewports. Radix's SheetPortal mounts
  // its overlay to document.body, so the `lg:hidden` on SheetContent alone does
  // NOT suppress the fixed inset-0 bg-black/40 overlay — it would dim the whole
  // desktop layout whenever rightPaneOpen flips true on lg+ widths.
  const isBelowLg = useIsMobile(1024);
  // Fallback focus target when the evidence pane closes without a citation
  // chip to return to (issue #508 / PRODUCT-ENH-10).
  const rightPaneToggleRef = useRef<HTMLButtonElement>(null);
  const messages = useChatMessages();
  const { open: shortcutsOpen, setOpen: setShortcutsOpen } = useKeyboardShortcuts();
  // Mobile Sheet uses its own state, toggled by the same button
  const [mobileSheetOpen, setMobileSheetOpen] = useState(false);
  type ResizeCleanup = (commitPending: boolean) => void;
  const activeResizeCleanupRef = useRef<ResizeCleanup | null>(null);

  const beginResizeGesture = useCallback(
    (
      mode: "mouse" | "touch",
      startWidth: number,
      widthFromClientX: (clientX: number) => number,
      applyWidth: (width: number) => void
    ) => {
      activeResizeCleanupRef.current?.(false);

      const originalCursor = document.body.style.cursor;
      const originalUserSelect = document.body.style.userSelect;
      let active = true;
      let pendingWidth = startWidth;
      let frame: number | null = null;

      const scheduleWidth = (clientX: number) => {
        if (!active) return;
        pendingWidth = widthFromClientX(clientX);
        if (frame !== null) return;
        frame = window.requestAnimationFrame(() => {
          if (!active || activeResizeCleanupRef.current !== cleanup) return;
          frame = null;
          applyWidth(pendingWidth);
        });
      };

      const onMouseMove = (moveEvent: MouseEvent) => {
        scheduleWidth(moveEvent.clientX);
      };
      const onMouseUp = () => cleanup(true);
      const onTouchMove = (moveEvent: TouchEvent) => {
        if (moveEvent.touches.length !== 1) {
          cleanup(false);
          return;
        }
        moveEvent.preventDefault();
        scheduleWidth(moveEvent.touches[0].clientX);
      };
      const onTouchEnd = () => cleanup(true);
      const onTouchCancel = () => cleanup(false);
      const onBlur = () => cleanup(false);

      function cleanup(commitPending: boolean) {
        if (!active) return;
        active = false;
        const ownsLifecycle = activeResizeCleanupRef.current === cleanup;

        if (mode === "mouse") {
          document.removeEventListener("mousemove", onMouseMove);
          document.removeEventListener("mouseup", onMouseUp);
        } else {
          document.removeEventListener("touchmove", onTouchMove);
          document.removeEventListener("touchend", onTouchEnd);
          document.removeEventListener("touchcancel", onTouchCancel);
        }
        window.removeEventListener("blur", onBlur);

        if (frame !== null) {
          window.cancelAnimationFrame(frame);
          frame = null;
          if (commitPending) applyWidth(pendingWidth);
        }

        if (ownsLifecycle) {
          document.body.style.cursor = originalCursor;
          document.body.style.userSelect = originalUserSelect;
          activeResizeCleanupRef.current = null;
        }
      }

      activeResizeCleanupRef.current = cleanup;
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
      window.addEventListener("blur", onBlur);
      if (mode === "mouse") {
        document.addEventListener("mousemove", onMouseMove);
        document.addEventListener("mouseup", onMouseUp);
      } else {
        document.addEventListener("touchmove", onTouchMove, { passive: false });
        document.addEventListener("touchend", onTouchEnd);
        document.addEventListener("touchcancel", onTouchCancel);
      }
    },
    []
  );

  useEffect(
    () => () => {
      activeResizeCleanupRef.current?.(false);
    },
    []
  );

  const handleExportChat = useCallback(() => {
    if (messages.length === 0) return;

    const chatText = messages
      .map((m) => `### ${m.role === "user" ? "User" : "Assistant"}\n\n${m.content}`)
      .join("\n\n---\n\n");

    // Build evidence appendices for assistant messages that have citations.
    const appendices: string[] = [];
    messages.forEach((m, idx) => {
      if (m.role !== "assistant") return;
      const msgLabel = `Message ${idx + 1}`;
      const wikiLines: string[] = [];
      const srcLines: string[] = [];
      const memLines: string[] = [];

      (m.wikiRefs ?? []).forEach((w) => {
        wikiLines.push(`[${w.wiki_label}] ${w.title} (${w.page_type ?? "wiki"}) — ${w.claim_text ?? w.excerpt ?? ""}`);
      });
      (m.sources ?? []).forEach((s) => {
        srcLines.push(`[${s.source_label ?? "S?"}] ${s.filename}${s.section ? ` § ${s.section}` : ""}`);
      });
      (m.memoriesUsed ?? []).forEach((mem) => {
        memLines.push(`[${mem.memory_label}] ${mem.content.slice(0, 200)}`);
      });

      if (wikiLines.length + srcLines.length + memLines.length === 0) return;
      const parts: string[] = [`#### ${msgLabel} — Evidence`];
      if (wikiLines.length) parts.push("**Wiki [W#]:**\n" + wikiLines.join("\n"));
      if (srcLines.length) parts.push("**Documents [S#]:**\n" + srcLines.join("\n"));
      if (memLines.length) parts.push("**Memories [M#]:**\n" + memLines.join("\n"));
      appendices.push(parts.join("\n\n"));
    });

    const fullText = appendices.length
      ? `${chatText}\n\n---\n\n## Evidence Appendix\n\n${appendices.join("\n\n---\n\n")}`
      : chatText;

    const blob = new Blob([fullText], { type: "text/markdown" });
    const url = URL.createObjectURL(blob);
    try {
      const link = document.createElement("a");
      link.href = url;
      link.download = `chat-${new Date().toISOString().slice(0, 10)}.md`;
      link.style.display = "none";
      document.body.appendChild(link);
      link.click();
      setTimeout(() => {
        if (document.body.contains(link)) document.body.removeChild(link);
        URL.revokeObjectURL(url);
      }, 100);
    } catch {
      URL.revokeObjectURL(url);
    }
  }, [messages]);

  const handleToggleSessionRail = () => {
    if (isMobile) {
      setMobileSheetOpen((prev) => !prev);
    } else {
      toggleSessionRail();
    }
  };

  // Sync URL sessionId → shell store activeSessionId
  useEffect(() => {
    if (sessionId && sessionId !== activeSessionId) {
      setActiveSessionId(sessionId);
    } else if (!sessionId && activeSessionId) {
      setActiveSessionId(null);
    }
  }, [sessionId, activeSessionId, setActiveSessionId]);

  // RT-04 fix: Load session messages when sessionId changes
  const loadedSessionRef = useRef<string | null>(null);
  // Monotonic token for in-flight transcript loads so a stale fetch can never
  // overwrite a newer selection's transcript.
  const loadSeqRef = useRef(0);
  useEffect(() => {
    if (!sessionId) {
      // Clear the marker so a delete-then-undo refetch of the same id re-runs
      // coherently instead of being skipped as "already loaded".
      loadedSessionRef.current = null;
      // New chat: evidence selection belongs to the previous session.
      useChatShellStore.getState().resetEvidenceSelection();
      return;
    }
    if (sessionId === loadedSessionRef.current) return;
    // Don't reload if we already have messages for this session
    const { activeChatId } = useChatStore.getState();
    if (activeChatId === sessionId) {
      loadedSessionRef.current = sessionId;
      return;
    }
    // Switching to a DIFFERENT session: drop the old session's evidence
    // selection (jump anchor + focus target) before the new transcript loads.
    // Same-id loads and clearMessages never reach this line.
    useChatShellStore.getState().resetEvidenceSelection();
    loadedSessionRef.current = sessionId;
    const seq = ++loadSeqRef.current;
    (async () => {
      try {
        if (testMode) {
          useChatStore.getState().loadChat(sessionId, mockChatMessages);
          return;
        }
        const detail = await getChatSession(parseInt(sessionId));
        if (seq !== loadSeqRef.current) return; // a newer selection superseded this fetch (UI-001)
        const loadedMessages: Message[] = (detail.messages ?? []).map(mapSessionMessage);
        useChatStore.getState().loadChat(sessionId, loadedMessages);
      } catch (err) {
        console.error("Failed to load chat session:", err);
      }
    })();
  }, [sessionId, testMode]);

  // UI-037: the mobile Sheet is only mounted on mobile viewports, so its
  // controlled open state cannot silently re-open the sheet when the viewport
  // returns to mobile after the sheet was left open.
  useEffect(() => {
    if (!isMobile) setMobileSheetOpen(false);
  }, [isMobile]);

  // PRODUCT-ENH-10 (issue #508): Escape closes the evidence pane (desktop
  // aside or mobile drawer) and restores focus — AFTER the close, via rAF so
  // the DOM is stable — to the citation chip that opened it, falling back to
  // the pane toggle button.
  useEffect(() => {
    if (!rightPaneOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      closeRightPane();
      requestAnimationFrame(() => {
        const chip = evidenceReturnFocusId
          ? document.querySelector(`[data-citation-chip-message="${evidenceReturnFocusId}"]`)
          : null;
        if (chip instanceof HTMLElement) {
          chip.focus();
        } else {
          rightPaneToggleRef.current?.focus();
        }
      });
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [rightPaneOpen, closeRightPane, evidenceReturnFocusId]);

  const handleResizeStart = (e: React.MouseEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startWidth = rightPaneWidth;
    beginResizeGesture(
      "mouse",
      startWidth,
      (clientX) => Math.max(320, Math.min(600, startWidth + startX - clientX)),
      setRightPaneWidth
    );
  };

  const handleSessionRailResizeStart = (e: React.MouseEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startWidth = sessionRailWidth;
    beginResizeGesture(
      "mouse",
      startWidth,
      (clientX) => Math.max(240, Math.min(400, startWidth + clientX - startX)),
      setSessionRailWidth
    );
  };

  // Keyboard resize step (px) — single grid unit. The store setters clamp
  // the final value to the legal range (useChatShellStore:95-96), so we
  // don't re-implement clamping here.
  const KEYBOARD_RESIZE_STEP = 16;

  // Keyboard parity for the session-rail resize handle. Drag-consistent
  // direction: ArrowRight grows the rail (matches onMouseMove sign at
  // line 224: delta = clientX - startX). WCAG 2.1.1 (Keyboard).
  const handleSessionRailKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowRight") {
      e.preventDefault();
      setSessionRailWidth(sessionRailWidth + KEYBOARD_RESIZE_STEP);
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      setSessionRailWidth(sessionRailWidth - KEYBOARD_RESIZE_STEP);
    }
  };

  // Keyboard parity for the right-pane resize handle. Drag-consistent
  // direction: ArrowLeft grows the pane (matches onMouseMove sign at
  // line 190: delta = startX - clientX, drag-left = wider).
  const handleResizeKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      setRightPaneWidth(rightPaneWidth + KEYBOARD_RESIZE_STEP);
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      setRightPaneWidth(rightPaneWidth - KEYBOARD_RESIZE_STEP);
    }
  };

  // Touch-drag parity for the session-rail resize handle. Document-level
  // touchmove/touchend listeners are attached with { passive: false } so
  // preventDefault actually suppresses page scroll (React 17+ attaches
  // onTouchStart as a passive root listener, so e.preventDefault() inside
  // the React handler is a no-op — the suppression has to happen on the
  // document listener). removeEventListener needs no options: per the DOM
  // spec, listener identity is (type, callback, capture) only.
  const handleSessionRailTouchStart = (e: React.TouchEvent) => {
    if (e.touches.length !== 1) {
      activeResizeCleanupRef.current?.(false);
      return;
    }
    const startX = e.touches[0].clientX;
    const startWidth = sessionRailWidth;
    beginResizeGesture(
      "touch",
      startWidth,
      (clientX) => Math.max(240, Math.min(400, startWidth + clientX - startX)),
      setSessionRailWidth
    );
  };

  // Touch-drag parity for the right-pane resize handle (sign matches the
  // mouse handler at line 190: delta = startX - clientX).
  const handleResizeTouchStart = (e: React.TouchEvent) => {
    if (e.touches.length !== 1) {
      activeResizeCleanupRef.current?.(false);
      return;
    }
    const startX = e.touches[0].clientX;
    const startWidth = rightPaneWidth;
    beginResizeGesture(
      "touch",
      startWidth,
      (clientX) => Math.max(320, Math.min(600, startWidth + startX - clientX)),
      setRightPaneWidth
    );
  };

  // Per-pane ErrorBoundary fallbacks — FR-017: isolate pane failures
  // SC-046: Retry resets the ErrorBoundary state (pane remount) instead of full page reload.
  const sessionsFallback = (reset: () => void) => (
    <div className="flex flex-col items-center justify-center min-h-[50vh] p-8">
      <div className="w-full max-w-md">
        <ErrorState
          title="Sessions error"
          description="The sessions panel encountered a problem. Try again to restore it."
          action={{ label: "Retry", onClick: reset }}
        />
      </div>
    </div>
  );

  const transcriptFallback = (reset: () => void) => (
    <div className="flex flex-col items-center justify-center min-h-[50vh] p-8">
      <div className="w-full max-w-md">
        <ErrorState
          title="Chat area error"
          description="The chat area encountered a problem. Try again to restore it."
          action={{ label: "Retry", onClick: reset }}
        />
      </div>
    </div>
  );

  const sourcesFallback = (reset: () => void) => (
    <div className="flex flex-col items-center justify-center min-h-[50vh] p-8">
      <div className="w-full max-w-md">
        <ErrorState
          title="Sources error"
          description="The sources panel encountered a problem. Try again to restore it."
          action={{ label: "Retry", onClick: reset }}
        />
      </div>
    </div>
  );

  return (
    <div className="flex h-full w-full overflow-hidden">
      {/* DESKTOP: Session Rail (persistent sidebar) */}
      <aside
        className={cn(
          "relative hidden md:flex md:flex-col md:shrink-0 md:border-r md:border-border md:bg-background md:transition-all md:duration-300 md:ease-in-out",
          sessionRailOpen ? "md:translate-x-0 md:opacity-100" : "md:w-0 md:opacity-0 md:overflow-hidden"
        )}
        style={{ width: sessionRailOpen ? `${sessionRailWidth}px` : "0px" }}
        aria-label="Chat sessions"
      >
        <ErrorBoundary fallback={sessionsFallback}>
          <SessionRail />
        </ErrorBoundary>
        {/* eslint-disable-next-line jsx-a11y-x/no-noninteractive-element-interactions -- role="separator" with resize handlers is the APG window-splitter pattern (keyboard + touch parity provided). */}
        <div
          className="absolute right-0 top-0 bottom-0 w-1 cursor-col-resize hover:bg-primary/20 active:bg-primary/40 transition-colors touch-none"
          onMouseDown={handleSessionRailResizeStart}
          onTouchStart={handleSessionRailTouchStart}
          onKeyDown={handleSessionRailKeyDown}
          role="separator"
          aria-label="Resize session panel"
          aria-orientation="vertical"
          aria-valuemin={240}
          aria-valuemax={400}
          aria-valuenow={sessionRailWidth}
          tabIndex={sessionRailOpen ? 0 : -1}
        />
      </aside>

      {/* MOBILE: Session Rail Sheet (slides from left). The Sheet ROOT stays
          mounted on every viewport (PRR-009): unmounting it on the breakpoint
          flip used to bypass Radix's close lifecycle, so focus was never
          restored to the trigger. Driving `open` with mobileSheetOpen &&
          isMobile keeps the desktop layout clean (UI-037 — closed Radix
          renders no portal/overlay) while letting the breakpoint flip run
          Radix's real close path and restore focus. */}
      <Sheet
        open={mobileSheetOpen && isMobile}
        onOpenChange={(open) => !open && setMobileSheetOpen(false)}
      >
        <SheetContent side="left" className="w-[280px] p-0 md:hidden" aria-describedby="chat-sessions-desc">
          <SheetHeader className="sr-only">
            <SheetTitle id="chat-sessions-title">Chat Sessions</SheetTitle>
            <SheetDescription id="chat-sessions-desc">Navigate between chat sessions</SheetDescription>
          </SheetHeader>
          <div className="flex h-full flex-col">
            <ErrorBoundary fallback={sessionsFallback}>
              <SessionRail />
            </ErrorBoundary>
          </div>
        </SheetContent>
      </Sheet>

      {/* MAIN TRANSCRIPT AREA */}
      <main className="flex flex-1 flex-col min-w-0 bg-background">
        <header className="flex h-14 items-center gap-2 border-b border-border px-4">
          {/* Page-level heading landmark (sr-only) so screen-reader heading
              navigation has an h1 on this route (UI-HIER-1, #291). */}
          <h1 className="sr-only">Chat</h1>
          {/* Session rail toggle — visible on all screen sizes */}
          <Button variant="ghost" size="icon" onClick={handleToggleSessionRail}
            aria-label={isMobile ? (mobileSheetOpen ? "Hide sessions" : "Show sessions") : (sessionRailOpen ? "Hide sessions" : "Show sessions")}
            aria-pressed={isMobile ? mobileSheetOpen : sessionRailOpen}>
            <PanelLeft className="h-5 w-5" aria-hidden="true" />
          </Button>
          {/* Active session title */}
          {activeSessionTitle && (
            <span className="flex-1 truncate text-sm font-medium text-foreground/80" title={activeSessionTitle}>
              {activeSessionTitle}
            </span>
          )}
          {!activeSessionTitle && <div className="flex-1" />}
          <VaultSelector />
          <Button variant="ghost" size="icon" onClick={handleExportChat}
            disabled={messages.length === 0}
            aria-label="Export chat">
            <Download className="h-5 w-5" aria-hidden="true" />
          </Button>
          <Button ref={rightPaneToggleRef} variant="ghost" size="icon" onClick={toggleRightPane}
            aria-label={rightPaneOpen ? "Hide details panel" : "Show details panel"}
            aria-pressed={rightPaneOpen}>
            <PanelRight className="h-5 w-5" aria-hidden="true" />
          </Button>
        </header>
        <div className="flex-1 overflow-hidden">
          <ErrorBoundary fallback={transcriptFallback}>
            <TranscriptPane />
          </ErrorBoundary>
        </div>
        {/* MOBILE: Safe area padding for iOS */}
        <div className="md:hidden" style={{ paddingBottom: 'env(safe-area-inset-bottom, 0px)' }} aria-hidden="true" />
      </main>

      {/* DESKTOP: Right Pane (persistent resizable sidebar) */}
      <aside
        className={cn(
          "relative hidden lg:flex lg:flex-col lg:shrink-0 lg:border-l lg:border-border lg:bg-background lg:transition-all lg:duration-300 lg:ease-in-out",
          rightPaneOpen ? "lg:translate-x-0 lg:opacity-100 blur-none" : "lg:w-0 lg:opacity-0 lg:overflow-hidden blur-xs"
        )}
        style={{ width: rightPaneOpen ? `${rightPaneWidth}px` : undefined }}
        aria-label="Details panel"
      >
        {rightPaneOpen && (
          // eslint-disable-next-line jsx-a11y-x/no-noninteractive-element-interactions -- APG window-splitter pattern (keyboard + touch parity provided; rendered only when rightPaneOpen is true so never a focusable-invisible tab stop).
          <div
            className="absolute left-0 top-0 bottom-0 w-1 cursor-col-resize hover:bg-primary/20 active:bg-primary/40 transition-colors hidden lg:block touch-none"
            onMouseDown={handleResizeStart}
            onTouchStart={handleResizeTouchStart}
            onKeyDown={handleResizeKeyDown}
            role="separator"
            aria-label="Resize details panel"
            aria-orientation="vertical"
            aria-valuemin={320}
            aria-valuemax={600}
            aria-valuenow={rightPaneWidth}
            tabIndex={0}
          />
        )}
        <div className="flex h-full flex-col p-4 bg-card/80 shrink-0 w-full">
          <ErrorBoundary fallback={sourcesFallback}>
            <RightPane />
          </ErrorBoundary>
        </div>
      </aside>

      {/* MOBILE: Right Pane Sheet (slides from bottom, 75vh). Non-modal
          (PRODUCT-ENH-10): no overlay, no focus trap — composer and transcript
          stay interactive while the evidence drawer is open. */}
      {isBelowLg && (
        <Sheet modal={false} open={rightPaneOpen} onOpenChange={(open) => !open && closeRightPane()}>
          <SheetContent side="bottom" overlay={false} className="h-[75vh] rounded-t-xl p-0 lg:hidden" aria-describedby="evidence-sources-desc">
            <SheetHeader className="px-4 pt-4 pb-2 border-b border-border">
              <SheetTitle id="evidence-sources-title" className="text-base text-left">Evidence</SheetTitle>
              <SheetDescription id="evidence-sources-desc" className="sr-only">
                View retrieved evidence and source documents
              </SheetDescription>
            </SheetHeader>
            <div className="absolute right-4 top-4 z-10">
              <SheetClose asChild>
                <Button variant="ghost" size="icon" className="h-8 w-8" aria-label="Close details panel">
                  <X className="h-4 w-4" aria-hidden="true" />
                </Button>
              </SheetClose>
            </div>
            <div className="flex h-full flex-col p-4 pt-2">
              <ErrorBoundary fallback={sourcesFallback}>
                <RightPane />
              </ErrorBoundary>
            </div>
          </SheetContent>
        </Sheet>
      )}

      {/* Keyboard Shortcuts Dialog */}
      <KeyboardShortcutsDialog open={shortcutsOpen} onOpenChange={setShortcutsOpen} />
    </div>
  );
}
