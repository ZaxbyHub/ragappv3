# Issue #772 group editor recovery

- Keep group member and vault editor state bound to the opening that started the read or save, including same-group reopenings.
- Keep initial editor reads fail-closed: pending, failed, or paused reads cannot initialize an empty full-replacement payload or enable Save. An initial read error offers Retry while Save remains disabled.
- Preserve these exact pre-initialization checks: `group-editor-recovery.test.tsx` — `fails closed for an initial member read error`, `fails closed for an initial vault catalog read error`, `shows an offline empty-read message and keeps member Save disabled`, and `shows an offline empty-read message and keeps vault Save disabled`; `ManageMembersSheet.l01.test.tsx` — `disables Save when the group members read fails`; `ManageMembersSheet.l02.test.tsx` — `keeps Save disabled when the eligible-members read fails`; `ManageVaultsSheet.l01.test.tsx` — `disables Save when the group vault-access read fails` and `disables Save while the initial vault-access read has no data`; `ManageVaultsSheet.l02.test.tsx` — `keeps Save disabled when the vault catalog read fails`.
- After an opening initializes, a failed background refresh preserves local edits and keeps Save enabled for the last loaded snapshot; show the actionable warning and Retry without overwriting that payload. This is the intentional post-initialization Save-enabled policy change.
- A post-initialization paused/offline state without an error does not show the refresh-failed warning; the initial uninitialized paused state shows the offline copy and keeps Save disabled.
- Block real Radix Sheet dismissal while full-replacement save is pending so X, Escape, outside dismissal, and Cancel cannot reopen a stale same-group snapshot. After the deferred save settles, the same X/Escape/outside path remains a working close path.
- Cover the same-group pending-read close/reopen regression: token 1 remains pending, the editor closes, token 2 reopens, the shared read settles, and token 2 initializes without token 1 local state.
- Preserve vault permissions/member replacement payloads through positive behavior coverage, page token forwarding assertions, and isolated query mocks.

Example validation commands (to run from `frontend/` after the follow-up is
applied):

```text
npm test -- --maxWorkers=1 src/components/groups/group-editor-recovery.test.tsx src/components/groups/ManageMembersSheet.l01.test.tsx src/components/groups/ManageMembersSheet.l02.test.tsx src/components/groups/ManageVaultsSheet.l01.test.tsx src/components/groups/ManageVaultsSheet.l02.test.tsx src/pages/AdminGroupsPage.feedback.test.tsx
npm run typecheck
npm run lint
npm run build
```

The live issue-comment IDs `5968477016-F001` through `5968477016-F005` and the
grouped review-root IDs `F-001` through `F-008` are different namespaces.
This fragment refers to the live IDs with their `5968477016-` prefix and to
the grouped roots as `review_report:F-001` through `review_report:F-008`; no
bare `F-###` identifier is intended to merge those source systems. The
release fragment also records IA-003, CD-007, CS-004, and RP-005 as design
notes; no speculative refactor is included.

Validation includes the focused Vitest command above, frontend typecheck,
lint, test, and build, plus the existing issue #772 closure evidence.
Concurrent full-replacement write-ordering remains a separate architectural
follow-up requiring an explicit versioning or serialization decision.
