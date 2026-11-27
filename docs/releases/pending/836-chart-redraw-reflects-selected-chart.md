# Chart redraw reflects the selected chart (#836, Workstream K PR 9 of 9)

Issue: #836 (item 2 of the audit consolidation; tracker-recorded symptom
"[Follow-up] PR #835 review residuals: ... stale mermaid diagram on chart
change (pre-existing)").

## What changed

`MermaidDiagram` (`frontend/src/components/chat/MermaidDiagram.tsx`) now tags
its render output with the chart the draw was started for: the state holds
`{ chart, svg }` (success) / `{ chart, message }` (render or import failure)
instead of untyped `svg` / `error` strings, and only output whose chart tag
equals the currently selected `chart` prop is displayed. A per-draw
`drawChart` constant is captured at effect start (replacing the
`chartRef.current` read inside the promise chain, which could describe a
chart the draw was not started for).

Effect: when the `chart` prop changes and the redraw is pending, the previous
diagram and the previous error fallback both disappear immediately (the
loading state shows), and a late out-of-order completion of the older draw
cannot present output for a chart that is no longer selected. The final
displayed diagram and its `aria-label` always identify the selected chart.

The dynamic-import chain still carries its rejection handler, so the issue
#689 guardrail (`frontend/src/tests/dynamicImportRejection.a07.guardrail.test.ts`)
stays green, and the error-fallback identity
(`data-testid="mermaid-error"`, `role="alert"`) is unchanged, so
`frontend/src/components/chat/MermaidDiagram.fb.fallback-pin.test.tsx` stays
green.

Tests: new regression suite
`frontend/src/tests/chart_redraw_reflects_the_selected_chart.test.tsx`
(five tests, mermaid module-mocked with controllable deferreds):

1. stale diagram removed while the redraw for the selected chart is
   pending; only the newest diagram presented (RED on the pre-fix tree);
2. stale error removed while the redraw for the selected chart is pending
   (RED on the pre-fix tree);
3. out-of-order completions: the older draw resolving last cannot
   overwrite the newest diagram (final chart identity asserted by svg
   content and `aria-label`);
4. A→B→A with B pending: chart A's held output is shown for the
   reselected chart while chart A redraws, and the late B completion
   cannot replace it;
5. a render failure with an empty message shows the error fallback
   instead of leaving the loading spinner up forever (RED on the pre-fix
   tree).

The error fallback is now gated on whether a tagged error exists for the
selected chart, not on whether its message is a non-empty string. Before,
an `Error("")` left `error` falsy and the spinner stayed up.

## Why

AC1 of #836: "WHILE a chart redraw is pending, THE SYSTEM SHALL remove
stale output and present only the newest diagram after out-of-order
completions." On the pre-fix component, `svg`/`error` state from the
previous draw survived a `chart` prop change: while the new draw was
pending, the previous chart's diagram (or error) stayed on screen with an
`aria-label` naming the newly selected chart — a diagram/label mismatch
and stale output, exactly the tracker-recorded "stale mermaid diagram on
chart change" residual. The out-of-order-completion half was already
covered by the per-effect `cancelled` flag; the chart-tagged state makes
both halves a structural property of the display path rather than a
timing property of the promise chain.

The only production consumer is
`frontend/src/components/chat/MarkdownMessage.tsx` (````mermaid` code
blocks render `<MermaidDiagram chart={codeText} />`). That consumer builds
its react-markdown `components` object inline, so every MarkdownMessage
render gives the `pre` renderer a new identity and React remounts the
MermaidDiagram subtree with fresh state. Through that consumer the stale
output was therefore latent rather than reachable today. This change fixes
the component's own contract (AC1 is stated and tested at component
level), so any consumer that keeps the instance mounted across a `chart`
change gets the correct behavior. The remount itself is a separate,
pre-existing behavior and is tracked as a follow-up issue rather than
changed here.

## Migration steps

None. Component-internal state shape only; no props, testids, DOM roles,
or persistence surfaces changed.

## Known caveats

- Out-of-order protection for a *cancelled* draw relies on the per-effect
  `cancelled` flag (unchanged from the pre-fix design); the chart-tagged
  display is defense in depth, not a replacement.
- The loading state shows while a redraw is pending even when the previous
  chart's diagram was already rendered — by design (AC1 requires stale
  output removed), but it means a rapid chart A→B→A sequence re-renders
  chart A's draw even though an older identical draw may have completed
  (no render-result memoization was added; mermaid is mocked in tests so
  real re-render cost is untested here). In that A→B→A case, if chart A's
  earlier output is still held (chart B's draw had not completed), it is
  shown immediately while chart A's redraw runs instead of the loading
  state. That output is tagged for chart A, the selected chart, so it is
  not stale and its `aria-label` matches.
- The out-of-order test (case 3) also passes on the pre-fix tree, because
  the `cancelled` flag already drops the older draw. Cases 1 and 2 are the
  ones that fail without this change.
