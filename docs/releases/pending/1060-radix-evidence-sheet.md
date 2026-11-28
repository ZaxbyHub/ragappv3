# fix(frontend): lift @radix-ui/react-dialog to ^1.3.1 (#1060)

## What changed

- `frontend/package.json` / `frontend/package-lock.json`:
  `@radix-ui/react-dialog` `^1.1.23` → `^1.3.1` (the pin PR #1212 added
  because 1.2+ broke the evidence-sheet e2e spec).
- `frontend/e2e/k08-evidence-sheet-snap.spec.ts`: the sheet locator now
  word-matches the description id (`[aria-describedby~="evidence-sources-desc"]`)
  instead of exact-equality matching, and a new #1060 AC1 test asserts the
  real-primitive contract — dialog role, composed `aria-describedby` list,
  Evidence heading, "Sources (1)" tab, and PRODUCT-ENH-10 focus return to
  the citation chip anchor after Escape.
- `frontend/src/pages/ChatShell.tsx`: the PRODUCT-ENH-10 close-and-restore
  logic moved into a shared `closePaneAndRestoreFocus` callback, and the
  mobile evidence `SheetContent` now consumes Escape via Radix's
  `onEscapeKeyDown`. Radix's DismissableLayer swallows Escape at
  document-capture with `preventDefault()`, which stood the window-level
  handler down whenever the sheet was mounted — so focus return never
  actually ran on the mobile drawer. The sheet now closes and restores
  focus itself; the desktop-aside path is unchanged.
- `.github/dependabot.yml`: removed the `@radix-ui/react-dialog`
  `>=1.2.0` ignore entry PR #1212 parked for this issue.

## Why

Issue #1060 (Workstream P, PR 2 of 2): WHEN the frontend lock resolves
`@radix-ui/react-dialog` to 1.3.1 or later, the chat evidence sheet must
render after the citation chip is clicked. The 8/8 e2e failures at 1.3.1
were not a missing sheet — the Playwright a11y tree showed the `dialog`
with its Evidence heading and Sources tab — but selector brittleness:
radix-dialog 1.2.0 added multiple-`aria-describedby` support, composing
the ids into a space-separated list, so the exact-match
`[aria-describedby="evidence-sources-desc"]` stopped matching. Writing the
focus-return assertion for the new AC exposed the second, real defect:
PRODUCT-ENH-10's focus restore was dead on the mobile sheet because Radix
consumed the Escape key before ChatShell's window-level listener.

## Migration steps

None. `npm ci` picks up the new dialog version from the lockfile; the
Sheet component API (`frontend/src/components/ui/sheet.tsx`) is unchanged,
and the dependabot hold removal just lets minor/patch radix bumps ride the
npm-minor-patch group again.

## Known caveats

- Anything that exact-match queries `aria-describedby` on Radix content
  will break on 1.2+ the same way this spec did. The repo-wide grep found
  only the (now word-matching) e2e spec and the ChatShell source that
  *sets* the attribute; no other consumer exists.
- The jsdom tier mocks the Sheet (`ChatShell.test.tsx`), so the
  real-Radix dialog semantics and focus-return contract are covered only
  by `frontend/e2e/k08-evidence-sheet-snap.spec.ts` — it must keep
  running in the Playwright CI job.
- Verified at resolved `1.3.1` (8 legacy tests + 1 new AC test pass) and
  at the `1.1.23` baseline before the bump (8/8 pass); versions beyond
  1.3.1 inherit the same word-match contract.
