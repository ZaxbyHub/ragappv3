// frontend/src/lib/shortcutBindings.ts
// Issue #573 (AC4): per-browser keyboard-shortcut rebinding, persisted under a
// localStorage key (consistent with the repo's kv-* persistence convention).
// The persisted shape is a flat map of shortcut id -> combo string, e.g.
// { "showShortcuts": "F9" }. Bindings are read at event time so a fresh hook
// mount honors whatever is persisted without waiting for store hydration.

export const SHORTCUT_BINDINGS_STORAGE_KEY = "kv-keyboard-shortcuts";

/** shortcutId -> combo string ("" never stored; absent = default). */
export type ShortcutBindings = Record<string, string>;

export function loadShortcutBindings(): ShortcutBindings {
  try {
    const raw = window.localStorage.getItem(SHORTCUT_BINDINGS_STORAGE_KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      return {};
    }
    const bindings: ShortcutBindings = {};
    for (const [id, combo] of Object.entries(parsed as Record<string, unknown>)) {
      if (typeof combo !== "string" || combo.length === 0) {
        continue;
      }
      // Issue #775: a showShortcuts combo in the palette's reserved set is
      // dead state — focusSearch's default claims it on /chat and the palette
      // claims it everywhere else — so a value persisted by a pre-#775 build
      // is ignored here rather than stranding the shortcut (the dialog and
      // its Reset control would otherwise be unreachable). Review PRR-203:
      // the dead entry is dropped from the NEXT write too — every write path
      // rebuilds from this filtered view — so it does not survive an
      // unrelated rebind; until then it stays unread in storage and every
      // consumer falls back to the default.
      if (id === "showShortcuts" && PALETTE_TOGGLE_COMBOS.has(combo)) {
        continue;
      }
      bindings[id] = combo;
    }
    return bindings;
  } catch {
    // Corrupt or unavailable storage falls back to defaults.
    return {};
  }
}

export function saveShortcutBinding(id: string, combo: string): void {
  try {
    const next = { ...loadShortcutBindings(), [id]: combo };
    window.localStorage.setItem(
      SHORTCUT_BINDINGS_STORAGE_KEY,
      JSON.stringify(next)
    );
  } catch {
    // Persistence failures must not break the rebind interaction itself.
  }
}

export function clearShortcutBindings(): void {
  try {
    window.localStorage.removeItem(SHORTCUT_BINDINGS_STORAGE_KEY);
  } catch {
    // Ignore unavailable storage.
  }
}

/** Drop a single shortcut's override (its holder reverts to the default). */
export function clearShortcutBinding(id: string): void {
  try {
    const next = loadShortcutBindings();
    if (!(id in next)) return;
    delete next[id];
    if (Object.keys(next).length === 0) {
      window.localStorage.removeItem(SHORTCUT_BINDINGS_STORAGE_KEY);
    } else {
      window.localStorage.setItem(
        SHORTCUT_BINDINGS_STORAGE_KEY,
        JSON.stringify(next)
      );
    }
  } catch {
    // Ignore unavailable storage.
  }
}

/** The combo currently bound to `id`, or `fallback` (the shipped default). */
export function effectiveBinding(id: string, fallback: string): string {
  return loadShortcutBindings()[id] ?? fallback;
}

const MODIFIER_KEYS = new Set(["Shift", "Control", "Alt", "Meta", "Dead", "Process"]);

/**
 * Normalize a keyboard event into a comparable combo string, e.g. "?" (a
 * printable char has its modifiers baked in), "F9", "ArrowUp", or
 * "Ctrl+K" (Ctrl stands in for Cmd on mac layouts, matching the dialog's
 * existing "Ctrl/Cmd" presentation). Returns null for bare modifier presses
 * and for Escape (reserved as the capture-cancel gesture), so neither can be
 * captured as a binding.
 */
export function comboFromEvent(e: { key: string; ctrlKey: boolean; metaKey: boolean }): string | null {
  const key = e.key;
  if (key === "Escape" || MODIFIER_KEYS.has(key)) return null;
  if (e.ctrlKey || e.metaKey) {
    if (key.length === 1) {
      return `Ctrl+${key.toUpperCase()}`;
    }
    return `Ctrl+${key}`;
  }
  return key;
}

/**
 * Issue #775: the command palette's toggle combos, in comboFromEvent's
 * normalized form (Ctrl stands in for Cmd). Single definition shared by the
 * palette's own keydown handler, the rebind-capture shadow refusal, and the
 * read-time guard in loadShortcutBindings — exactly one owner per combo.
 */
export const PALETTE_TOGGLE_COMBOS: ReadonlySet<string> = new Set(["Ctrl+K"]);

/**
 * True when an event target is a text-entry surface (issue #775). Tolerates
 * non-Element targets (window/document) — they are never editable.
 */
export function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  // isContentEditable is explicitly compared (not just returned) so the
  // function always yields a real boolean even where a DOM implementation
  // reports the property as undefined (jsdom does).
  return (
    target.tagName === "INPUT" ||
    target.tagName === "TEXTAREA" ||
    target.tagName === "SELECT" ||
    target.isContentEditable === true
  );
}

/**
 * Issue #775: build a KeyboardEventInit whose comboFromEvent normalization
 * round-trips back to `combo` — used by the palette's "Show keyboard
 * shortcuts" action to drive the single existing shortcut dispatcher instead
 * of opening the dialog through a second channel.
 */
export function comboToKeyboardEventInit(combo: string): KeyboardEventInit {
  // cancelable so the receiving listener's preventDefault() is effective
  // (jsdom and browsers default KeyboardEvent to non-cancelable).
  if (combo.startsWith("Ctrl+")) {
    const key = combo.slice("Ctrl+".length);
    return { key, ctrlKey: true, cancelable: true };
  }
  return { key: combo, cancelable: true };
}

/** Shared execution guard for window/document shortcut owners. */
export function canHandleShortcutEvent(e: Pick<KeyboardEvent, "defaultPrevented" | "repeat" | "isComposing" | "target">): boolean {
  return !e.defaultPrevented && !e.repeat && !e.isComposing && !isEditableTarget(e.target);
}
