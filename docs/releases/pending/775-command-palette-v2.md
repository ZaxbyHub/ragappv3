# 775: Command palette v2 — every destination reachable, and exactly one owner per Ctrl+K

Issue: #775 ([Workstream L] PR 4 of 9; frontier audit run 20260923T174456Z
proposal UI-ENH-08 with folded findings TQ-sibling-batch-03-04 and
TQ-sibling-batch-03-05).

## What changed

- **Exactly one owner per Ctrl+K.** The command palette's window handler now
  yields to any earlier claimant (`e.defaultPrevented`) and toggles only on
  the shared `PALETTE_TOGGLE_COMBOS` definition (`Ctrl/Cmd+K`,
  comboFromEvent-normalized); the chat rail's `focusSearch` listener does the
  same. On `/chat` one Ctrl+K now focuses the session search (document-level
  listener wins) and the palette stays closed; everywhere else it opens the
  palette. The second `ChatSearchInput` instance (mobile sheet) also yields
  to the first. All global keydown consumers additionally skip IME
  composition.
- **Rebind capture can no longer persist a combo that cannot fire.** The
  `showShortcuts` capture refuses combos that remain shadowed after the
  conflict resolver runs — focusSearch's post-clear effective binding or the
  palette's reserved combo (both collapse to `Ctrl+K` today) — leaving the
  capture armed, exactly like a bare modifier press. A read-time guard in
  `loadShortcutBindings` also ignores a pre-existing persisted
  `showShortcuts: "Ctrl+K"` (writable by pre-#775 builds) so that state can
  no longer strand the shortcuts dialog behind a dead binding; the guard
  never writes, and an explicit Reset still clears the stale entry.
  Stealing a *persisted* focusSearch combo (e.g. F7) still works — the holder
  reverts to its default, which fires.
- **A persisted showShortcuts chord now fires.** The `showShortcuts`
  consumer's blanket Ctrl/Meta early-return is replaced by exact-combo
  matching (chords normalize to `Ctrl+<KEY>` in `comboFromEvent`), so a
  rebind like Ctrl+J actually opens the dialog instead of being persisted
  forever unfired. The default `?` behavior and the input/textarea guard are
  unchanged (Ctrl+`?` still does not open: `Ctrl+?` ≠ `?`).
- **A printable or editing-key rebind can no longer steal focus from an
  editor.** `focusSearch`'s listener skips ALL modifier-free combos when the
  event target is an editable surface — printables, Enter, Tab, Backspace,
  arrows. Modifier combos (the Ctrl/Cmd+K default and any Ctrl-rebind) keep
  focusing search from inside the composer exactly as before.
- **The palette reaches every destination.** Navigation commands derive from
  `NavigationRail`'s canonical `navItems` export under the rail's own
  visibility rule (`isAdminRole` + `isNavItemVisible`, both exported) — 12
  destinations for an admin with Draft Room enabled, exactly the 9 non-admin
  destinations for a member, never a `/admin/*` path for non-admins. No
  hand-copied subset remains.
- **The palette runs actions and searches entities.** Five non-navigating
  actions ship: Show keyboard shortcuts (drives the single existing
  dispatcher by dispatching the bound combo), Toggle light/dark theme, Use
  system theme, Use high contrast theme, and Copy page link. Queries of 2+
  characters debounce (300 ms) into the existing unified search API
  (`/search/unified`, #515) with a stale-response guard; entity hits render
  below the commands and navigate to their `url_hint`. Render order is
  pinned: destinations, then actions, then hits.
- **Palette navigation honors unsaved changes.** Both palette navigation
  paths — destination commands and entity-hit rows — consult the shared
  `useNavigationGuardStore.confirmLeave` guard before `navigate()`, the same
  contract the mobile bottom nav and App.tsx's item-select dispatch follow
  (implementation-review round 1). A declined confirmation leaves the
  current route and any dirty page (e.g. an unsaved Draft Room canvas)
  intact; confirming navigates normally.
- **The composer's slash/attach buttons are in the Tab order**
  (`tabIndex={-1}` removed; both keep their accessible names and behavior).
- **Contract evolution (named per plan-critic round 1/3):** the
  keyboard-shortcuts surface ("?" listener + dialog) moved from ChatShell to
  the app shell (`App.tsx` `AppShortcutsMount`), so "?" works on every route
  and the palette's shortcuts action is wired app-wide. Chat-page behavior is
  unchanged — the same listener and dialog, one level up, mounted exactly
  once. The #573 source-scan guardrail in `chat-parity-preserving.test.ts`
  was retargeted accordingly (asserts the single mount lives in App.tsx and
  ChatShell no longer mounts a second copy).

## Verification

Frozen checks C1-C10 (issue-tracer trace `.agents/issue-traces/775-command-palette-v2`,
anchor comments on #775): C1-C8 RED→GREEN, C9/C10 PRESERVING green before and
after. New suites: `shortcutBindings.roundtrip.issue775.test.ts` (4 — combo
round-trips, `isEditableTarget`, read-time guard),
`KeyboardShortcuts.shadowed-capture.issue775.test.tsx` (5 — shadow-refused
capture, Enter-in-editor, stranded-state fallback, F7-steal pin, Ctrl+J
persists-and-fires pin), `command-palette-actions.issue775.test.tsx` (5 —
shortcuts action through the app-wide hook, theme side effects, entity-hit
navigation, destinations-before-actions DOM order, declined-guard blocks
palette navigation) — 14/14.
Sibling suites re-run green: `command-palette.issue258`,
`KeyboardShortcuts.rebind`/`.test`/`.capture`, `SessionRail.rebind`,
`Composer.slash-button`, `chat-parity-preserving`, `ChatShell` suites.

## Known limitations

- A pre-fix stranded `showShortcuts: "Ctrl+K"` stays in localStorage (the
  read-time guard ignores it without writing) until an explicit Reset
  shortcuts; the shortcut itself immediately falls back to `?`.
- The palette's own toggle combo is reserved and not user-rebindable — the
  shipped contract, now enforced at capture time instead of silently
  shadowed.
