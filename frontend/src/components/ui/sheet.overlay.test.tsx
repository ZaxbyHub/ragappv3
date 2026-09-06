/**
 * @vitest-environment jsdom
 */
import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import {
  Sheet,
  SheetContent,
  SheetTitle,
  SheetDescription,
} from "./sheet";

// Radix portals the overlay into document.body; identify it by the dimming
// background class SheetOverlay applies.
const findOverlay = () =>
  Array.from(document.body.querySelectorAll("div")).find((el) =>
    (el.getAttribute("class") ?? "").includes("bg-black/40")
  );

function renderSheet(props: { overlay?: boolean }) {
  return render(
    <Sheet open onOpenChange={() => undefined}>
      <SheetContent side="bottom" overlay={props.overlay}>
        <SheetTitle>Sheet title</SheetTitle>
        <SheetDescription>Sheet description</SheetDescription>
      </SheetContent>
    </Sheet>
  );
}

describe("SheetContent overlay prop (issue #508 WU-12)", () => {
  it("renders the dimming overlay by default (backward compatible)", () => {
    renderSheet({});
    expect(findOverlay()).toBeDefined();
  });

  it("omits the overlay entirely when overlay={false}", () => {
    renderSheet({ overlay: false });
    expect(findOverlay()).toBeUndefined();
  });
});
