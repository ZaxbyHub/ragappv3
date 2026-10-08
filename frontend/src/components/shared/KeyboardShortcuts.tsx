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
  isEditableTarget,
  PALETTE_TOGGLE_COMBOS,
  saveShortcutBinding,
  clearShortcutBinding,
  clearShortcutBindings,
  loadShortcutBindings,
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

/** Canonical (comboFromEvent-normalized) forms of the rebindable defaults —
 * used for conflict comparison, since stored bindings are normalized. */
const CANONICAL_DEFAULT: Partial<Record<ShortcutId, string>> = {
  focusSearch: "Ctrl+K",
  showShortcuts: "?",
};

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
      // Issue #775: one owner per combo — if an earlier listener already
      // claimed this event (e.g. focusSearch on /chat), never double-fire.
      if (e.defaultPrevented) return;
      // IME composition is not a shortcut gesture (repo discipline, cf.
      // useEscapeToStop).
      if (e.isComposing) return;
      // Show shortcuts on the bound combo (default "?"). Bindings are read at
      // event time so a fresh mount honors whatever is persisted. Modifier
      // chords normalize to "Ctrl+<key>" in comboFromEvent, so a combo only
      // matches when the persisted binding literally holds that chord — a
      // rebind to Ctrl+J therefore fires (issue #775: capture and firing
      // rules must agree). Never trigger while typing in inputs.
      const combo = comboFromEvent(e);
      if (combo === null || combo !== bindingFor("showShortcuts")) return;
      const target = e.target;
      if (!isEditableTarget(target)) {
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
    // Reset on BOTH transitions: entering capture with a stale state would
    // be confusing, and leaving the dialog open->false while capturing MUST
    // disarm the window capture listener — the dialog stays mounted
    // (ChatShell renders it unconditionally), so without this reset the
    // listener would swallow the next app-wide keypress and silently persist
    // it as a rebind (PRR-001).
    setCapturing(null);
    if (open) {
      setBindings((prev) => ({ ...prev }));
    }
  }, [open]);

  useEffect(() => {
    if (!capturing) return;
    const handleCapture = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.repeat || e.isComposing) return;
      e.preventDefault();
      e.stopPropagation();
      const combo = comboFromEvent(e);
      // null = bare modifier press or Escape (cancel) — keep waiting on
      // modifiers, cancel on Escape.
      if (combo === null) {
        if (e.key === "Escape") setCapturing(null);
        return;
      }
      // Issue #775 shadow refusal — runs BEFORE the conflict-clearing loop so
      // a refused capture leaves persisted state untouched. A combo that
      // could never fire must not be persisted: after the resolver runs, the
      // combo is still claimed when ANOTHER rebindable shortcut's post-clear
      // effective binding equals it (a steal of a persisted combo reverts
      // that holder to its default, so only a combo equal to the holder's
      // default remains claimed), or — for showShortcuts — when the combo is
      // in PALETTE_TOGGLE_COMBOS (the palette's app-wide window listener
      // claims it off /chat; the reserve is one-directional because
      // focusSearch capturing its OWN shipped default "Ctrl+K" is an
      // identity rebind that fires fine and was always allowed).
      const current = loadShortcutBindings();
      let shadowed = capturing === "showShortcuts" && PALETTE_TOGGLE_COMBOS.has(combo);
      if (!shadowed) {
        for (const other of shortcuts) {
          if (other.id === capturing || !other.rebindable) continue;
          const otherDefault: string = CANONICAL_DEFAULT[other.id] ?? other.key;
          const otherEffective: string = current[other.id] ?? otherDefault;
          const otherPostClear: string =
            otherEffective === combo ? otherDefault : otherEffective;
          if (otherPostClear === combo) {
            shadowed = true;
            break;
          }
        }
      }
      if (shadowed) {
        // Swallowed like a bare modifier press: capture stays armed.
        return;
      }
      // Conflict handling (PRR-003): if another rebindable shortcut already
      // holds this combo, clear that binding so the combo drives exactly one
      // action (the previous holder reverts to its default). Comparison uses
      // canonical combos — stored bindings are comboFromEvent-normalized, and
      // the rebindable defaults have canonical forms ("Ctrl/Cmd + K" →
      // "Ctrl+K", "?" → "?").
      for (const other of shortcuts) {
        if (other.id === capturing || !other.rebindable) continue;
        const otherCanonical = CANONICAL_DEFAULT[other.id] ?? other.key;
        const otherEffective = current[other.id] ?? otherCanonical;
        if (otherEffective === combo) {
          clearShortcutBinding(other.id);
          setBindings((prev) => ({ ...prev, [other.id]: other.key }));
        }
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
                {/* aria-live so capture entry (the "…" placeholder) and the
                    saved combo are announced to screen readers (PRR-006/007). */}
                <dt className="font-mono text-sm bg-muted px-2 py-1 rounded-sm" aria-live="polite">
                  {isCapturingRow ? "…" : shownKey}
                </dt>
                <dd className="flex min-w-0 flex-1 items-center justify-end gap-2 text-sm text-muted-foreground">
                  {isCapturingRow ? (
                    <span className="text-xs italic" data-testid="capture-hint" role="status">
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
