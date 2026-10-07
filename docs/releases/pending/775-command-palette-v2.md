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
  to the first, and a HIDDEN instance never claims the combo at all: the
  desktop rail stays mounted while hidden (display:none below `md`, or
  collapsed to w-0/opacity-0), so ownership now resolves to the visible
  instance — the visible sheet input where one exists, otherwise the
  palette. Where the browser lacks `checkVisibility` the pre-#775 claim
  behavior applies. All global keydown consumers additionally skip IME
  composition.
- **Rebind capture can no longer persist a combo that cannot fire.** Rebind
  capture refuses any combo that remains shadowed after the conflict
  resolver runs — still claimed by another rebindable shortcut's post-clear
  effective binding (today that collapses to `Ctrl+K`), and for
  `showShortcuts` also the palette's reserved combo — leaving the capture
  armed, exactly like a bare modifier press, in BOTH directions (the reverse
  steal of `?` for `focusSearch` is refused; re-capturing `focusSearch`'s
  own shipped default `Ctrl+K` stays allowed — an identity rebind that
  fires). A read-time guard in `loadShortcutBindings` also ignores a
  pre-existing persisted `showShortcuts: "Ctrl+K"` (writable by pre-#775
  builds) so that state can no longer strand the shortcuts dialog behind a
  dead binding; the guard never writes — the dead entry is dropped from
  storage the next time any rebind is saved. Stealing a *persisted* combo
  whose holder reverts to a different default (e.g. F7) still works — the
  old holder's default fires.
- **Closing the palette with Ctrl/Cmd+K resets it** like every other close
  path (Escape, outside click, executing a command) — the next open starts
  with an empty query and no stale entity hits, and no in-flight search
  lands into a closed palette.
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
  destinations for a member with the capability (8 without it), never a
  `/admin/*` path for non-admins. No hand-copied subset remains.
- **The palette runs actions and searches entities.** Five non-navigating
  actions ship: Show keyboard shortcuts (drives the single existing
  dispatcher by dispatching the bound combo), Toggle light/dark theme, Use
  system theme, Use high contrast theme, and Copy page link (with
  copied/failed toast feedback on every outcome). Queries of 2+
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
  intact — the palette itself is dismissed either way, so re-open with
  Ctrl/Cmd+K to retry; confirming navigates normally.
- **The composer's slash/attach buttons are in the Tab order**
  (`tabIndex={-1}` removed; both keep their accessible names and behavior).
- **Contract evolution (named per plan-critic round 1/3):** the
  keyboard-shortcuts surface ("?" listener + dialog) moved from ChatShell to
  the app shell (`App.tsx` `AppShortcutsMount`), so "?" works on every shell
  route (the pre-auth setup/login/register and forced-password-change
  screens render without the shell) and the palette's shortcuts action is
  wired app-wide. Chat-page behavior is
  unchanged — the same listener and dialog, one level up, mounted exactly
  once. The #573 source-scan guardrail in `chat-parity-preserving.test.ts`
  was retargeted accordingly (asserts the single mount lives in App.tsx and
  ChatShell no longer mounts a second copy).

## Verification

Frozen checks C1-C10 (issue-tracer trace `.agents/issue-traces/775-command-palette-v2`,
anchor comments on #775): C1-C8 RED→GREEN, C9/C10 PRESERVING green before and
after. Issue-775 pin suites (29 tests at the final head):
`shortcutBindings.roundtrip.issue775.test.ts` (4 — combo round-trips,
`isEditableTarget`, read-time guard),
`KeyboardShortcuts.shadowed-capture.issue775.test.tsx` (10 — shadow-refused
capture, Enter-in-editor, stranded-state fallback, F7-steal pin, Ctrl+J
persists-and-fires pin, reverse-steal refusal, identity-rebind allowed, IME,
defaultPrevented, Ctrl+K-in-composer carve-out),
`command-palette-actions.issue775.test.tsx` (11 — shortcuts action through
the app-wide hook incl. a non-default rebind, theme side effects + resolvedDark
direction, entity-hit navigation + declined guard on BOTH navigation sites,
destinations-before-actions DOM order, toggle-close reset + closure,
copy-link toasts, min-char/empty state),
`ctrl-k-visible-owner.issue775.test.tsx` (3 — visible-owner rule incl. the
hidden-only and no-`checkVisibility` fallbacks),
`command-palette-capability-off.issue775.test.tsx` (1 — capability-OFF
exclusion), `app-shortcuts-shell.issue775.test.tsx` (1 — "?" through the real
App shell).
Sibling suites re-run green: `command-palette.issue258`,
`KeyboardShortcuts.rebind`/`.test`/`.capture`, `SessionRail.rebind`,
`Composer.slash-button`, `chat-parity-preserving`, `ChatShell` suites.
Check-id mapping (trace ids C1-C10 → behaviors; shipped files self-label
where noted): ctrl-k-ownership.l04 = C1/AC1; KeyboardShortcuts.l04 =
C2+C3/AC2+AC3; command-palette.l04 = C4-C7/AC4-AC7; Composer.l04 = C8/AC8;
command-palette.issue258 = C9/AC9; KeyboardShortcuts.rebind = C10/AC10.

## Known limitations

- A pre-fix stranded `showShortcuts: "Ctrl+K"` is ignored at read time (the
  shortcut immediately falls back to `?`) and is dropped from storage the
  next time any rebind is saved; until then it remains unread in storage.
- A refused rebind is silent by design — the capture stays armed exactly as
  if a bare modifier had been pressed; there is no rejection toast.
- A shortcut deliberately rebound to a bare editing key (e.g. Tab) still
  fires on that key everywhere OUTSIDE text-entry surfaces (inside editors
  non-modifier combos never fire) — choose rebinds accordingly.
- The visible-owner rule for `focusSearch` uses `checkVisibility` where the
  browser provides it. Older browsers without that API keep the pre-#775
  claim behavior: the hidden instance still claims Ctrl+K and the keypress
  does nothing visible (the pre-fix symptom persists there).
- The palette's own toggle combo is reserved and not user-rebindable — the
  shipped contract, now enforced at capture time instead of silently
  shadowed.
