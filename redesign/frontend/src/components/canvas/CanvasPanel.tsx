"use client";

import React from "react";
import { motion, AnimatePresence } from "framer-motion";
import { DocumentText, Code2, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { useChatStore } from "@/stores/useChatStore";
import { DocumentPreview } from "./DocumentPreview";
import { CodeViewer } from "./CodeViewer";
import { ResizableHandle } from "./ResizableHandle";
import { Button } from "@/components/ui/button";

export function CanvasPanel() {
  const {
    canvas,
    toggleCanvasCollapse,
    setCanvasWidth,
    setCanvasView,
    sessions,
    currentSessionId,
  } = useChatStore();

  const currentSession = sessions.find((s) => s.id === currentSessionId);
  const lastAssistantMessage = currentSession?.messages.findLast((m) => m.role === "assistant");
  const sources = lastAssistantMessage?.sources || [];

  const activeSource = sources.find((s) => s.id === canvas.activeSourceId) || sources[0];

  const tabs = [
    { id: "document" as const, label: "Document", icon: DocumentText },
    { id: "code" as const, label: "Code", icon: Code2 },
  ];

  return (
    <AnimatePresence>
      {!canvas.isCollapsed && (
        <motion.div
          initial={{ width: 0, opacity: 0 }}
          animate={{ width: canvas.width, opacity: 1 }}
          exit={{ width: 0, opacity: 0 }}
          transition={{ duration: 0.3, ease: "easeInOut" }}
          className="relative h-full bg-background border-l border-border flex flex-col"
        >
          <ResizableHandle onResize={setCanvasWidth} />

          <div className="flex items-center justify-between px-4 py-3 border-b border-border">
            <div className="flex gap-1">
              {tabs.map((tab) => (
                <button
                  key={tab.id}
                  onClick={() => setCanvasView(tab.id)}
                  className={cn(
                    "flex items-center gap-2 px-3 py-1.5 text-sm rounded transition-colors",
                    canvas.view === tab.id
                      ? "bg-accent text-accent-foreground"
                      : "text-muted-foreground hover:text-foreground"
                  )}
                >
                  <tab.icon className="h-4 w-4" />
                  {tab.label}
                </button>
              ))}
            </div>
            <Button variant="ghost" size="icon" onClick={toggleCanvasCollapse}>
              <X className="h-4 w-4" />
            </Button>
          </div>

          <div className="flex-1 overflow-auto p-4">
            {canvas.view === "document" && activeSource && (
              <DocumentPreview source={activeSource} />
            )}
            {canvas.view === "code" && activeSource && (
              <CodeViewer source={activeSource} />
            )}
            {!activeSource && (
              <div className="h-full flex items-center justify-center text-muted-foreground">
                <p className="text-sm">No document to preview</p>
              </div>
            )}
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}