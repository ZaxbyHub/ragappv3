import { useState, useEffect } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Keyboard } from "lucide-react";

const shortcuts = [
  { key: "Enter", description: "Send message" },
  { key: "Shift + Enter", description: "New line in message" },
  { key: "Ctrl/Cmd + Enter", description: "Send message (alternative)" },
  { key: "Ctrl/Cmd + K", description: "Focus session search" },
  { key: "↑ / ↓", description: "Navigate sessions (when search focused)" },
  { key: "?", description: "Show keyboard shortcuts" },
  { key: "Esc", description: "Close dialogs / Stop streaming" },
];

export function useKeyboardShortcuts() {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      // Show shortcuts on ? key (but not when typing in inputs)
      if (e.key === "?" && !e.shiftKey && !e.ctrlKey && !e.metaKey) {
        const target = e.target as HTMLElement;
        if (target.tagName !== "INPUT" && target.tagName !== "TEXTAREA" && !target.isContentEditable) {
          e.preventDefault();
          setOpen(true);
        }
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  return { open, setOpen };
}

export function KeyboardShortcutsDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md" aria-labelledby="keyboard-shortcuts-title" aria-describedby="keyboard-shortcuts-desc">
        <DialogHeader>
          <DialogTitle id="keyboard-shortcuts-title" className="flex items-center gap-2">
            <Keyboard className="w-5 h-5" />
            Keyboard Shortcuts
          </DialogTitle>
          <DialogDescription id="keyboard-shortcuts-desc">
            Available keyboard shortcuts for quick navigation
          </DialogDescription>
        </DialogHeader>
        <dl className="space-y-3 mt-4">
          {shortcuts.map(({ key, description }) => (
            <div key={key} className="flex justify-between items-center">
              <dt className="font-mono text-sm bg-muted px-2 py-1 rounded-sm">{key}</dt>
              <dd className="text-sm text-muted-foreground">{description}</dd>
            </div>
          ))}
        </dl>
      </DialogContent>
    </Dialog>
  );
}
