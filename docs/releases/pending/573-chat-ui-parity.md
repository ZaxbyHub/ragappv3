# feat(frontend): close chat UI parity gaps (#573)

## What changed
- **Follow-up suggestions (E17/AC1):** after the newest completed assistant turn, the transcript offers up to three suggested next questions (`FollowUpSuggestions`), derived deterministically from that turn's already-retrieved context (its source titles plus the user's question) — no new retrieval call and no extra LLM call. Clicking a suggestion sends it as the next message.
- **Continue generation (E17/AC2):** the SSE `done` event's `llm_metrics.finish_reason` is now surfaced through `parseSSEStream` (`onFinishReason` callback) onto the assistant message. When it is `"length"` (max_tokens truncation), the transcript offers a **Continue** action that resends the truncated content as prior context (the partial answer stays in history and the model resumes instead of restarting).
- **Version navigation (E17/AC3):** editing an earlier turn now snapshots the pre-edit content client-side (transcript-slot keyed maps in `useChatStore`), and the edited message renders an inline `1 / N` stepper (`VersionStepper`) to step between sibling versions. Display-only swap; the fork/lineage data model is untouched.
- **Shortcut rebinding (E17/AC4):** the existing `KeyboardShortcutsDialog` gains per-shortcut rebinding (capture a new combo) with per-browser persistence under the `kv-keyboard-shortcuts` localStorage key, plus a reset control. The two window-level shortcuts are rebindable: dialog open (default `?`) and session-search focus (default `Ctrl/Cmd+K`; the SessionRail consumer honors overrides). Composer-internal Enter-family combos stay fixed (IME safety).
- **Share link (E17/AC5):** a **Share conversation link** header action (`ShareAction`) copies `${origin}${appPath("/chat/<id>")}` to the clipboard — basename-aware for subpath deployments. **Design decision (per the issue's trust-boundary mandate):** this is an authenticated-only session link — every chat API requires an authenticated user, so the URL renders the conversation only for signed-in users of this deployment, and deleting the session revokes it. No public/unauthenticated surface, no new trust boundary, no database change. A tokenized public-share mechanism is explicitly out of scope.
- **WCAG 2.2 2.5.8 target-size audit (E17/AC6):** `frontend/src/tests/target-size.audit.test.tsx` computes effective target boxes from Tailwind size/padding tokens for the bespoke icon-only controls that bypass the shared `Button` `icon` variant. The audit established four failing controls at base — Composer attachment-remove (12×12), WikiCards open (10×10), KMSCards open (10×10), Sheet close (16×16) — and all four now meet the 24×24 minimum; the two code-block `CopyButton`s were already at exactly 24×24 and are pinned there.
- **Playwright e2e smoke tier (E17/AC7):** new `frontend/e2e/` suite (own `package.json`+`package-lock.json` (locking `@playwright/test` 1.63.0), zero-dependency `node:http` stub backend on `:9090` implementing the real client contract — auth/CSRF/login/refresh/me, vaults, sessions, durable-turn batch writes, progressive SSE with `SLOW`/`LENGTH` markers) driving the production build via `vite preview` (new `preview.proxy` wiring) through four scenarios: send; stop mid-stream with the interrupted turn persisted server-side and restored on reload; reload restores a completed exchange; citation chip opens the source popover and details pane. New sibling CI job `e2e` (Node 22.14.0, chromium, 20-min timeout), separate from the existing Frontend gates.
- **Verified shipped, not reimplemented (per the issue's verify-before-build correction):** the Markdown export (`ChatShell.tsx` `handleExportChat`) and the `?` shortcut-discoverability overlay (`KeyboardShortcuts.tsx`) already exist at base and were not rebuilt.

## Why
Workstream L, PR 4 of 4 — closes the remaining chat-UI parity gaps from the 2026-09-11 frontier audit (REPORT.md §12.4, finding E17) and adds the browser-level regression gate the 20.5k-line chat surface lacked.

## Tests
- Frozen acceptance checks C1–C9 (issue-tracer checkpoint, receipt anchored on the issue): component contracts + wiring scans for the five features, the DISCRIMINATING target-size audit (red at base with the four failing controls), PRESERVING guards for export/overlay singularity, `role="log"`/`aria-live`, and the fork data model.
- `SessionRail.rebind.test.tsx` pins the search-focus rebinding consumer.
- `frontend/e2e/chat-smoke.spec.ts` (4 tests) runs in the new CI job.

## Caveats
- `finish_reason` and edit-version snapshots are transient client state — not persisted to session rows (no backend schema change), so both are absent after a reload by design.
- Follow-up suggestions are heuristic (deterministic templates over retrieved titles + the question), not model-generated.
- Rebinding is per-browser (localStorage), not per-account — consistent with the repo's existing preference persistence; documented in the dialog.

## Rollback
Each item is an additive component/config change; a plain revert restores the previous surface. Nothing touches the database or durable-storage schemas.
