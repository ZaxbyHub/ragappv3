import { render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { ErrorBoundary } from "./ErrorBoundary";

// Issue #779 feedback (PRR-012 / review F-1): the resetOnChange state
// machine had no direct unit pin — the frozen App-level tests only exercise
// the hasError+value-change branch (ErrorBoundary.tsx gDSFP line "cleared"),
// so the mutation "drop the resetOnChangeValue re-track on the healthy
// branch" shipped green. These tests cover all three getDerivedStateFromProps
// branches and the good-to-crash-good-again cycle that mutation kills.

function Thrower({ active }: { active: boolean }) {
  if (active) {
    throw new Error("boundary unit probe boom");
  }
  return <div>healthy content</div>;
}

// Harness: `resetKey` is the resetOnChange prop; `broken` flips the child
// between throwing and healthy so rerender drives every gDSFP branch.
function Harness({ resetKey, broken }: { resetKey: string; broken: boolean }) {
  return (
    <ErrorBoundary resetOnChange={resetKey}>
      <Thrower active={broken} />
    </ErrorBoundary>
  );
}

describe("ErrorBoundary resetOnChange (issue #779, UI-R2-06)", () => {
  it("shows the fallback when the child throws", () => {
    render(<Harness resetKey="a" broken />);
    expect(screen.getByText("Something went wrong")).toBeInTheDocument();
    expect(screen.queryByText("healthy content")).not.toBeInTheDocument();
  });

  it("clears the error when resetOnChange changes while broken (hasError branch)", () => {
    const { rerender } = render(<Harness resetKey="a" broken />);
    expect(screen.getByText("Something went wrong")).toBeInTheDocument();

    rerender(<Harness resetKey="b" broken={false} />);
    expect(screen.getByText("healthy content")).toBeInTheDocument();
    expect(screen.queryByText("Something went wrong")).not.toBeInTheDocument();
  });

  it("re-tracks the reset token on healthy navigations (healthy branch — two-cycle)", () => {
    // a: healthy -> b: healthy (healthy branch re-tracks the token to b) ->
    // b: crash -> a: healthy (recovery at a requires the b-retrack: under
    // the mutation "the healthy branch does not re-track", the stale token
    // still equals a, gDSFP's equality short-circuit swallows the reset,
    // and the fallback persists at a).
    const { rerender } = render(<Harness resetKey="a" broken={false} />);
    expect(screen.getByText("healthy content")).toBeInTheDocument();

    rerender(<Harness resetKey="b" broken={false} />);
    expect(screen.getByText("healthy content")).toBeInTheDocument();

    rerender(<Harness resetKey="b" broken />);
    expect(screen.getByText("Something went wrong")).toBeInTheDocument();

    rerender(<Harness resetKey="a" broken={false} />);
    expect(screen.getByText("healthy content")).toBeInTheDocument();
    expect(screen.queryByText("Something went wrong")).not.toBeInTheDocument();
  });

  it("does not clear the error on a same-value re-render (equality branch)", () => {
    // The equality short-circuit must NOT swallow a live error just because
    // the parent re-rendered with an unchanged key — otherwise a still-
    // broken page silently un-fallbacks and re-throws every render.
    const { rerender } = render(<Harness resetKey="a" broken />);
    expect(screen.getByText("Something went wrong")).toBeInTheDocument();

    rerender(<Harness resetKey="a" broken />);
    expect(screen.getByText("Something went wrong")).toBeInTheDocument();
  });

  it("leaves prop-less consumers on the base behavior (no reset without the prop)", () => {
    // App.tsx's top-level boundary and the five ChatShell boundaries pass no
    // resetOnChange: undefined === undefined must keep the caught error
    // across re-renders, byte-identical to the pre-#779 class.
    function NoPropHarness({ broken }: { broken: boolean }) {
      return (
        <ErrorBoundary>
          <Thrower active={broken} />
        </ErrorBoundary>
      );
    }
    const { rerender } = render(<NoPropHarness broken />);
    expect(screen.getByText("Something went wrong")).toBeInTheDocument();

    rerender(<NoPropHarness broken={false} />);
    expect(screen.getByText("Something went wrong")).toBeInTheDocument();
  });
});
