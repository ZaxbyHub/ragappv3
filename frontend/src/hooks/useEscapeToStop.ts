import { useEffect } from "react";

/**
 * While `active` (e.g. a response is streaming), pressing Escape invokes
 * `onStop`. Window-scoped so it works regardless of focus, but ignores Escape
 * presses already handled elsewhere (e.g. a Radix dialog/menu dismiss that
 * called `preventDefault`) so generation is not stopped just because a popover
 * was closed.
 */
export function useEscapeToStop(active: boolean, onStop: () => void): void {
  useEffect(() => {
    if (!active) return;
    const onKeyDown = (e: KeyboardEvent) => {
      // Defer to IME: Escape cancels an in-progress composition.
      if (e.isComposing) return;
      if (e.key === "Escape" && !e.defaultPrevented) {
        e.preventDefault();
        onStop();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [active, onStop]);
}
