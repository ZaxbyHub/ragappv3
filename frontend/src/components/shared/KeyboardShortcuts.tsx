import { useState, useEffect } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Keyboard, RotateCw } from "lucide-react";
import {
  comboFromEvent,
  effectiveBinding,
  saveShortcutBinding,
  clearShortcutBindings,
} from "@/lib/shortcutBindings";

/**
 * Issue #573 (AC4): shortcut entries carry stable ids; the two window-level
 * combos are rebindable (persisted per-browser under "kv-keyboard-shortcuts"
 * via @/lib/shortcutBindings). Composer-internal combos (the Enter family)
 * stay fixed — their listeners live inside the textarea with IME guards, and
 * rebinding them would risk breaking IME composition.
 */
const shortcuts = [
  { id: "sendMessage", key: "Enter", description: "Send message", rebindable: false },
  { id: "newLine", key: "Shift + Enter", description: "New line in message", rebindable: false },
  { id: "sendMessageAlt", key: "Ctrl/Cmd + Enter", description: "Send message (alternative)", rebindable: false },
  { id: "focusSearch", key: "Ctrl/Cmd + K", description: "Focus session search", rebindable: true },
  { id: "navigateSessions", key: "↑ / ↓", description: "Navigate sessions (when search focused)", rebindable: false },
  { id: "showShortcuts", key: "?", description: "Show keyboard shortcuts", rebindable: true },
  { id: "closeStop", key: "Esc", description: "Close dialogs / Stop streaming", rebindable: false },
] as const;

export type ShortcutId = (typeof shortcuts)[number]["id"];

/** Default combo for a shortcut id (the shipped binding). */
export function defaultBinding(id: ShortcutId): string {
  return shortcuts.find((s) => s.id === id)?.key ?? "";
}

/** Combo currently bound to a shortcut id — persisted override or default. */
export function bindingFor(id: ShortcutId): string {
  return effectiveBinding(id, defaultBinding(id));
}

export function useKeyboardShortcuts() {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      // Show shortcuts on the bound combo (default "?"). Bindings are read at
      // event time so a fresh mount honors whatever is persisted. Shift is
      // physically required to type "?" on US layouts, so printable-char
      // combos carry their shift inside the key itself; modifier combos
      // normalize to "Ctrl+<key>". Never trigger while typing in inputs.
      if (e.ctrlKey || e.metaKey) return;
      const combo = comboFromEvent(e);
      if (combo === null || combo !== bindingFor("showShortcuts")) return;
      const target = e.target as HTMLElement;
      if (target.tagName !== "INPUT" && target.tagName !== "TEXTAREA" && !target.isContentEditable) {
        e.preventDefault();
        setOpen(true);
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  return { open, setOpen };
}

export function KeyboardShortcutsDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  // Local mirror of the persisted bindings so rows re-render on rebind/reset.
  const [bindings, setBindings] = useState<Record<string, string>>({});
  const [capturing, setCapturing] = useState<ShortcutId | null>(null);

  useEffect(() => {
    if (open) {
      setBindings((prev) => ({ ...prev }));
      setCapturing(null);
    }
  }, [open]);

  useEffect(() => {
    if (!capturing) return;
    const handleCapture = (e: KeyboardEvent) => {
      e.preventDefault();
      e.stopPropagation();
      const combo = comboFromEvent(e);
      // null = bare modifier press or Escape (cancel) — keep waiting on
      // modifiers, cancel on Escape.
      if (combo === null) {
        if (e.key === "Escape") setCapturing(null);
        return;
      }
      saveShortcutBinding(capturing, combo);
      setBindings((prev) => ({ ...prev, [capturing]: combo }));
      setCapturing(null);
    };
    // Capture phase so the combo is recorded before any app-level listener.
    window.addEventListener("keydown", handleCapture, true);
    return () => window.removeEventListener("keydown", handleCapture, true);
  }, [capturing]);

  const resetBindings = () => {
    clearShortcutBindings();
    setBindings({});
    setCapturing(null);
  };

  const displayKey = (id: ShortcutId, fallback: string) =>
    bindings[id] ?? effectiveBinding(id, fallback);

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
          {shortcuts.map(({ id, key, description, rebindable }) => {
            const shownKey = displayKey(id, key);
            const isCapturingRow = capturing === id;
            return (
              <div key={key} className="flex justify-between items-center gap-2">
                <dt className="font-mono text-sm bg-muted px-2 py-1 rounded-sm">
                  {isCapturingRow ? "…" : shownKey}
                </dt>
                <dd className="flex min-w-0 flex-1 items-center justify-end gap-2 text-sm text-muted-foreground">
                  {isCapturingRow ? (
                    <span className="text-xs italic" data-testid="capture-hint">
                      Press the new key combination (Esc cancels)
                    </span>
                  ) : (
                    <span className="truncate">{description}</span>
                  )}
                  {rebindable && !isCapturingRow && (
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      className="h-6 w-6"
                      aria-label={`Rebind shortcut: ${description}`}
                      onClick={() => setCapturing(id)}
                    >
                      <RotateCw className="h-3.5 w-3.5" aria-hidden />
                    </Button>
                  )}
                </dd>
              </div>
            );
          })}
        </dl>
        <div className="mt-4 flex justify-end">
          <Button type="button" variant="outline" size="sm" onClick={resetBindings}>
            Reset shortcuts
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
