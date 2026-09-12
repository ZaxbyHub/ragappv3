// Regression checks for issue #494 AC37 / LIVE-10 + DEEP-D-03 and the
// PRESERVING node P-UI-PRESERVE.
//
// AC37: SaveDiscardFooter renders "Saving…" whenever dirtyCount === 0 &&
// !invalid inside a region that is only visually hidden (opacity) when
// invisible — never removed from the accessibility tree. So an idle,
// clean Settings page still exposes an "Unsaved changes" region with an
// unsolicited "Saving…" text to assistive tech, and the transient
// post-save window flashes "Saving…". This check asserts REQUIRED
// behavior that does not exist at the pre-fix base (a543361) and is
// expected to FAIL there; it prints an "AC37 CHECK: FAIL" sentinel
// immediately before the discriminating assertion. The true-positive
// saving indicator sub-assert (saving=true && clean) is green at base and
// after, and lives in the same node to pin it.
//
// P-UI-PRESERVE: the existing dirty-footer contract (count text, enabled
// Save/Discard, invalid hint) must stay green at base and after the fix.
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { SaveDiscardFooter } from "./SaveDiscardFooter";

const noop = () => {};

function footerElement(
  props: Partial<Parameters<typeof SaveDiscardFooter>[0]> = {},
) {
  return (
    <SaveDiscardFooter
      dirtyCount={0}
      invalid={false}
      saving={false}
      onSave={noop}
      onDiscard={noop}
      {...props}
    />
  );
}

describe("SaveDiscardFooter idle-state exposure (issue #494)", () => {
  it("AC37: idle footer must not expose 'Saving…' to the accessibility tree; dirty footer shows the count instead", () => {
    // Positive control — green at base and after the fix: while a save is
    // genuinely in flight with a clean form, the Saving indicator IS shown.
    const { rerender } = render(footerElement({ saving: true, dirtyCount: 0 }));
    expect(screen.getByText("Saving…")).toBeInTheDocument();

    // Idle: dirtyCount=0, saving=false, invalid=false.
    rerender(footerElement());

    console.log("AC37 CHECK: FAIL");

    const region = screen.queryByRole("region", { name: "Unsaved changes" });
    const regionHiddenFromA11y =
      region !== null &&
      (region.getAttribute("aria-hidden") === "true" ||
        region.hasAttribute("inert"));
    // Contract: no unsolicited "Saving…" while idle — either the text (or
    // the whole region) is not rendered, or the region is removed from the
    // accessibility tree (aria-hidden / inert). At base the text is in the
    // DOM inside a merely-opacity-hidden region.
    expect(screen.queryByText("Saving…") === null || regionHiddenFromA11y).toBe(
      true,
    );

    // Dirty + not saving: the footer reports the dirty count, never
    // "Saving…".
    rerender(footerElement({ dirtyCount: 1, saving: false, invalid: false }));
    expect(screen.getByText("1 unsaved change")).toBeInTheDocument();
    expect(screen.queryByText("Saving…")).toBeNull();
  });

  it("P-UI-PRESERVE: dirty footer keeps the count text, enabled Save/Discard buttons, and the invalid hint", () => {
    console.log("P-UI-PRESERVE: PRESERVING GREEN");
    const { rerender } = render(
      footerElement({ dirtyCount: 2, saving: false, invalid: false }),
    );

    expect(screen.getByText("2 unsaved changes")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save Changes" })).toBeEnabled();
    expect(
      screen.getByRole("button", { name: "Discard unsaved changes" }),
    ).toBeEnabled();

    // invalid=true surfaces the destructive validation hint.
    rerender(footerElement({ dirtyCount: 2, saving: false, invalid: true }));
    expect(
      screen.getByText("Fix highlighted errors before saving"),
    ).toBeInTheDocument();
  });
});
