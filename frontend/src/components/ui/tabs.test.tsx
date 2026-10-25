// frontend/src/components/ui/tabs.test.tsx
// #865 wrapper pin: TabsContent force-mounts every panel so each tab
// trigger's aria-controls resolves to an element in the DOM, while inactive
// panels are hidden (data-[state=inactive]:hidden base class) — out of
// layout, the tab order, and the accessibility tree in a real browser.

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "./tabs";

describe("ui/tabs forceMount wrapper (#865)", () => {
  it("keeps inactive panels mounted so aria-controls always resolves", () => {
    render(
      <Tabs defaultValue="a">
        <TabsList>
          <TabsTrigger value="a">Alpha</TabsTrigger>
          <TabsTrigger value="b">Beta</TabsTrigger>
        </TabsList>
        <TabsContent value="a">alpha panel</TabsContent>
        <TabsContent value="b">beta panel</TabsContent>
      </Tabs>
    );

    const alpha = screen.getByRole("tab", { name: "Alpha" });
    const beta = screen.getByRole("tab", { name: "Beta" });

    // Every trigger's aria-controls must resolve to a mounted element —
    // the dangling-id defect this wrapper eliminates.
    for (const trigger of [alpha, beta]) {
      const id = trigger.getAttribute("aria-controls");
      expect(id).toBeTruthy();
      expect(document.getElementById(id as string)).not.toBeNull();
    }

    // The inactive panel stays mounted, marked inactive, and carries the
    // display:none base class that keeps it out of layout and the a11y tree.
    const betaPanel = document.getElementById(
      beta.getAttribute("aria-controls") as string
    ) as HTMLElement;
    expect(betaPanel.getAttribute("data-state")).toBe("inactive");
    expect(betaPanel.className).toContain("data-[state=inactive]:hidden");
    expect(betaPanel.getAttribute("role")).toBe("tabpanel");
  });
});
