import { useEffect, useState, useCallback, useRef } from "react";
import { useParams } from "react-router-dom";
import { cn } from "@/lib/utils";
import { getChatSession } from "@/lib/api";
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
  } = useChatShellStore();

  const isMobile = useIsMobile();
  // Gate right-pane bottom Sheets on sub-lg viewports. Radix's SheetPortal mounts
  // its overlay to document.body, so the `lg:hidden` on SheetContent alone does
  // NOT suppress the fixed inset-0 bg-black/40 overlay — it would dim the whole
  // desktop layout whenever rightPaneOpen flips true on lg+ widths.
  const isBelowLg = useIsMobile(1024);
  const messages = useChatMessages();
  const { open: shortcutsOpen, setOpen: setShortcutsOpen } = useKeyboardShortcuts();
  // Mobile Sheet uses its own state, toggled by the same button
  const [mobileSheetOpen, setMobileSheetOpen] = useState(false);

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
  useEffect(() => {
    if (!sessionId || sessionId === loadedSessionRef.current) return;
    // Don't reload if we already have messages for this session
    const { activeChatId } = useChatStore.getState();
    if (activeChatId === sessionId) {
      loadedSessionRef.current = sessionId;
      return;
    }
    loadedSessionRef.current = sessionId;
    (async () => {
      try {
        if (testMode) {
          useChatStore.getState().loadChat(sessionId, mockChatMessages);
          return;
        }
        const detail = await getChatSession(parseInt(sessionId));
        const loadedMessages: Message[] = (detail.messages ?? []).map((m) => ({
          id: m.id.toString(),
          role: m.role as "user" | "assistant",
          content: m.content,
          sources: m.sources ?? undefined,
          memoriesUsed: m.memories ?? undefined,
          wikiRefs: m.wiki_refs ?? undefined,
          kmsRefs: m.kms_refs ?? undefined,
          mode: m.mode ?? undefined,
          created_at: m.created_at,
          feedback: m.feedback ?? null,
        }));
        useChatStore.getState().loadChat(sessionId, loadedMessages);
      } catch (err) {
        console.error("Failed to load chat session:", err);
      }
    })();
  }, [sessionId, testMode]);

  const handleResizeStart = (e: React.MouseEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startWidth = rightPaneWidth;
    let pendingWidth = startWidth;
    let frame: number | null = null;
    const onMouseMove = (moveEvent: MouseEvent) => {
      const delta = startX - moveEvent.clientX;
      pendingWidth = Math.max(240, Math.min(600, startWidth + delta));
      if (frame !== null) return;
      frame = window.requestAnimationFrame(() => {
        setRightPaneWidth(pendingWidth);
        frame = null;
      });
    };
    const onMouseUp = () => {
      if (frame !== null) {
        window.cancelAnimationFrame(frame);
        setRightPaneWidth(pendingWidth);
        frame = null;
      }
      document.removeEventListener("mousemove", onMouseMove);
      document.removeEventListener("mouseup", onMouseUp);
      window.removeEventListener("blur", onMouseUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    document.addEventListener("mousemove", onMouseMove);
    document.addEventListener("mouseup", onMouseUp);
    window.addEventListener("blur", onMouseUp);
  };

  const handleSessionRailResizeStart = (e: React.MouseEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startWidth = sessionRailWidth;
    let pendingWidth = startWidth;
    let frame: number | null = null;
    const onMouseMove = (moveEvent: MouseEvent) => {
      const delta = moveEvent.clientX - startX;
      pendingWidth = Math.max(200, Math.min(400, startWidth + delta));
      if (frame !== null) return;
      frame = window.requestAnimationFrame(() => {
        setSessionRailWidth(pendingWidth);
        frame = null;
      });
    };
    const onMouseUp = () => {
      if (frame !== null) {
        window.cancelAnimationFrame(frame);
        setSessionRailWidth(pendingWidth);
        frame = null;
      }
      document.removeEventListener("mousemove", onMouseMove);
      document.removeEventListener("mouseup", onMouseUp);
      window.removeEventListener("blur", onMouseUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    document.addEventListener("mousemove", onMouseMove);
    document.addEventListener("mouseup", onMouseUp);
    window.addEventListener("blur", onMouseUp);
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
          "relative hidden md:flex md:flex-col md:flex-shrink-0 md:border-r md:border-border md:bg-background md:transition-all md:duration-300 md:ease-in-out",
          sessionRailOpen ? "md:translate-x-0 md:opacity-100" : "md:w-0 md:opacity-0 md:overflow-hidden"
        )}
        style={{ width: sessionRailOpen ? `${sessionRailWidth}px` : "0px" }}
        aria-label="Chat sessions"
      >
        <ErrorBoundary fallback={sessionsFallback}>
          <SessionRail />
        </ErrorBoundary>
        <div
          className="absolute right-0 top-0 bottom-0 w-1 cursor-col-resize hover:bg-primary/20 active:bg-primary/40 transition-colors"
          onMouseDown={handleSessionRailResizeStart}
          role="separator"
          aria-label="Resize session panel"
          aria-orientation="vertical"
        />
      </aside>

      {/* MOBILE: Session Rail Sheet (slides from left) */}
      <Sheet open={mobileSheetOpen} onOpenChange={(open) => !open && setMobileSheetOpen(false)}>
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
          <Button variant="ghost" size="icon" onClick={toggleRightPane}
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
          "hidden lg:flex lg:flex-col lg:flex-shrink-0 lg:border-l lg:border-border lg:bg-background lg:transition-all lg:duration-300 lg:ease-in-out",
          rightPaneOpen ? "lg:translate-x-0 lg:opacity-100 blur-none" : "lg:w-0 lg:opacity-0 lg:overflow-hidden blur-sm"
        )}
        style={{ width: rightPaneOpen ? `${rightPaneWidth}px` : undefined }}
        aria-label="Details panel"
      >
        {rightPaneOpen && (
          <div className="relative left-0 top-0 bottom-0 w-1 cursor-col-resize hover:bg-primary/20 active:bg-primary/40 transition-colors hidden lg:block" onMouseDown={handleResizeStart} role="separator" aria-label="Resize details panel" aria-orientation="vertical" />
        )}
        <div className="flex h-full flex-col p-4 bg-card/80 flex-shrink-0 w-full">
          <ErrorBoundary fallback={sourcesFallback}>
            <RightPane />
          </ErrorBoundary>
        </div>
      </aside>

      {/* MOBILE: Right Pane Sheet (slides from bottom, 75vh) */}
      {isBelowLg && (
        <Sheet open={rightPaneOpen} onOpenChange={(open) => !open && closeRightPane()}>
          <SheetContent side="bottom" className="h-[75vh] rounded-t-xl p-0 lg:hidden" aria-describedby="evidence-sources-desc">
            <SheetHeader className="sr-only">
              <SheetTitle id="evidence-sources-title">Evidence & Sources</SheetTitle>
              <SheetDescription id="evidence-sources-desc">View retrieved evidence and source documents</SheetDescription>
            </SheetHeader>
            <div className="absolute right-4 top-4 z-10">
              <SheetClose asChild>
                <Button variant="ghost" size="icon" className="h-8 w-8" aria-label="Close details panel">
                  <X className="h-4 w-4" aria-hidden="true" />
                </Button>
              </SheetClose>
            </div>
            <div className="flex h-full flex-col p-4 pt-12">
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
