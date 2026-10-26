// frontend/src/components/ui/tabs.test.tsx
// #865 contract pin: Radix keeps every inactive TabsContent panel in the DOM
// as a hidden shell, so EVERY tab trigger's aria-controls resolves to an
// element in the document without any forceMount — and the hidden shell's
// content stays unmounted (out of layout, tab order, and the accessibility
// tree), which keeps the a11y matrix measuring exactly what renders.

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "./tabs";

describe("ui/tabs aria-controls resolution (#865)", () => {
  it("resolves every trigger's aria-controls via the hidden inactive panel", () => {
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

    // The #865 invariant: no trigger may reference a panel id that is absent
    // from the document.
    for (const trigger of [alpha, beta]) {
      const id = trigger.getAttribute("aria-controls");
      expect(id).toBeTruthy();
      expect(document.getElementById(id as string)).not.toBeNull();
    }

    // The active panel renders its content.
    const alphaPanel = document.getElementById(
      alpha.getAttribute("aria-controls") as string
    ) as HTMLElement;
    expect(alphaPanel.getAttribute("data-state")).toBe("active");
    expect(alphaPanel).toHaveTextContent("alpha panel");
    expect(alphaPanel.hasAttribute("hidden")).toBe(false);

    // The inactive panel is the hidden shell: present (so the id resolves),
    // marked inactive, but its CONTENT is unmounted — the same contract the
    // a11y matrix measures on every tabbed route.
    const betaPanel = document.getElementById(
      beta.getAttribute("aria-controls") as string
    ) as HTMLElement;
    expect(betaPanel.getAttribute("data-state")).toBe("inactive");
    expect(betaPanel.getAttribute("role")).toBe("tabpanel");
    expect(betaPanel.hasAttribute("hidden")).toBe(true);
    expect(betaPanel).not.toHaveTextContent("beta panel");
  });
});
