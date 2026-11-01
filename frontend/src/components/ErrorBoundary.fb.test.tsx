import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ErrorBoundary } from "@/components/ErrorBoundary";

// Supplemental feedback coverage for #779. The frozen C3/C4 checks prove the
// shell survives a page throw and recovers on navigation; these tests exercise
// the boundary state machine so a future branch cannot pass only because a
// fallback happens to render.

function ThrowingPage({ label }: { label: string }): never {
  throw new Error(`${label} render boom`);
}

function BoundaryHarness() {
  const [token, setToken] = useState("/healthy-a");
  const [crashing, setCrashing] = useState(false);

  return (
    <>
      <button type="button" onClick={() => setCrashing(true)}>
        Crash page
      </button>
      <button
        type="button"
        onClick={() => {
          setToken("/healthy-b");
          setCrashing(false);
        }}
      >
        Navigate healthy
      </button>
      <button
        type="button"
        onClick={() => {
          setToken("/crash-again");
          setCrashing(true);
        }}
      >
        Navigate crash again
      </button>
      <button type="button" onClick={() => setCrashing(false)}>
        Render healthy same token
      </button>
      <ErrorBoundary
        resetOnChange={token}
        fallback={(resetErrorBoundary) => (
          <div role="alert">
            <span>Route fallback</span>
            <button type="button" onClick={resetErrorBoundary}>
              Try Again
            </button>
          </div>
        )}
      >
        {crashing ? <ThrowingPage label={token} /> : <div>Healthy page</div>}
      </ErrorBoundary>
    </>
  );
}

describe("ErrorBoundary feedback state coverage (#779)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("covers healthy A to healthy B to crash B with the same reset token", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    render(<BoundaryHarness />);

    expect(screen.getByText("Healthy page")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Navigate healthy" }));
    expect(screen.getByText("Healthy page")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Crash page" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Route fallback");
  });

  it("requires Try Again when the same route token becomes healthy", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    render(<BoundaryHarness />);

    fireEvent.click(screen.getByRole("button", { name: "Crash page" }));
    expect(screen.getByRole("alert")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Render healthy same token" }));
    expect(screen.getByRole("alert")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Try Again" }));
    expect(screen.getByText("Healthy page")).toBeInTheDocument();
  });

  it("resets on path, query, or hash token changes and renders healthy children", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    function TokenHarness() {
      const [token, setToken] = useState("/documents?q=one#first");
      const [crashing, setCrashing] = useState(true);
      return (
        <>
          <button
            type="button"
            onClick={() => {
              setToken("/documents?q=two#first");
              setCrashing(false);
            }}
          >
            Change query
          </button>
          <button
            type="button"
            onClick={() => {
              setToken("/documents?q=two#second");
              setCrashing(false);
            }}
          >
            Change hash
          </button>
          <ErrorBoundary
            resetOnChange={token}
            fallback={<div role="alert">Route fallback</div>}
          >
            {crashing ? (
              <ThrowingPage label={token} />
            ) : (
              <div>{token.endsWith("#second") ? "Healthy after hash" : "Healthy after query"}</div>
            )}
          </ErrorBoundary>
        </>
      );
    }

    render(<TokenHarness />);
    expect(screen.getByRole("alert")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Change query" }));
    expect(screen.getByText("Healthy after query")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Change hash" }));
    expect(screen.getByText("Healthy after hash")).toBeInTheDocument();
  });

  it("observes the second thrown label when a changed route crashes again", () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    render(<BoundaryHarness />);
    fireEvent.click(screen.getByRole("button", { name: "Crash page" }));
    fireEvent.click(screen.getByRole("button", { name: "Navigate crash again" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Route fallback");
    expect(errorSpy.mock.calls.flat().some((value) =>
      String(value).includes("/crash-again render boom"),
    )).toBe(true);
  });
});

describe("ErrorBoundary resetOnChange contract (issue #779 PRR-012)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("does not retry the second caught error on an unrelated same-token rerender", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    let throwCount = 0;

    function Prr012ThrowingPage({ label }: { label: string }): never {
      throwCount += 1;
      throw new Error(`${label} render boom`);
    }

    function Prr012Harness() {
      const [token, setToken] = useState("A");
      const [crashing, setCrashing] = useState(false);
      const [, setParentTick] = useState(0);

      return (
        <>
          <button
            type="button"
            onClick={() => {
              setToken("B");
              setCrashing(true);
            }}
          >
            throw B
          </button>
          <button type="button" onClick={() => setToken("C")}>
            throw C
          </button>
          <button type="button" onClick={() => setParentTick((tick) => tick + 1)}>
            rerender parent
          </button>
          <ErrorBoundary
            resetOnChange={token}
            fallback={<div data-testid="prr012-fallback">caught</div>}
          >
            {crashing ? <Prr012ThrowingPage label={token} /> : <div>healthy</div>}
          </ErrorBoundary>
        </>
      );
    }

    render(<Prr012Harness />);

    fireEvent.click(screen.getByRole("button", { name: "throw B" }));
    expect(await screen.findByTestId("prr012-fallback")).toBeInTheDocument();
    const countAfterB = throwCount;
    expect(countAfterB).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: "throw C" }));
    expect(await screen.findByTestId("prr012-fallback")).toBeInTheDocument();
    const countAfterC = throwCount;
    expect(countAfterC).toBeGreaterThan(countAfterB);

    fireEvent.click(screen.getByRole("button", { name: "rerender parent" }));
    expect(screen.getByTestId("prr012-fallback")).toBeInTheDocument();
    expect(throwCount).toBe(countAfterC);
    expect(consoleError).toHaveBeenCalled();
  });
});
