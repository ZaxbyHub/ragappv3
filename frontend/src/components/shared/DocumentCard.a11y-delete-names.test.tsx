// src/components/shared/DocumentCard.a11y-delete-names.test.tsx
// Feedback round 2026-11-26 (F: DocumentCard mid-delete transient —
// execution-proven in real Chromium: with a STATIC accessible name
// "Delete Document (<file>)" and visible text "Deleting…" during
// isDeleting, axe 4.14.1 reports label-content-name-mismatch (serious) on
// the in-flight control. The accessible name must carry the state's own
// word so the visible text stays contained (WCAG 2.5.3).
//
// jsdom + RTL suffice here: the pin asserts the rendered aria-label per
// state (the rule mechanics were proven in the executed Chromium probe; the
// unit pin keeps the label state-aware against regressions).

import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";
import { describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { DocumentCard } from "@/components/shared/DocumentCard";

const baseDocument = {
  id: "doc-1",
  filename: "report.pdf",
  metadata: { status: "completed" },
};

function renderCard(isDeleting: boolean) {
  // DocumentCard renders react-router <Link>s — provide the router context.
  return render(
    <MemoryRouter>
      <DocumentCard document={baseDocument} onDelete={vi.fn()} isDeleting={isDeleting} canDelete />
    </MemoryRouter>,
  );
}

describe("DocumentCard delete accessible names (state-aware, WCAG 2.5.3)", () => {
  it("steady state: the delete button's name is 'Delete Document (<filename>)'", () => {
    renderCard(false);
    expect(screen.getByLabelText("Delete Document (report.pdf)")).toBeInTheDocument();
  });

  it("mid-delete: the name switches to 'Deleting Document (<filename>)' so the visible 'Deleting…' stays contained", () => {
    renderCard(true);
    expect(screen.getByLabelText("Deleting Document (report.pdf)")).toBeInTheDocument();
    expect(screen.queryByLabelText("Delete Document (report.pdf)")).not.toBeInTheDocument();
  });
});
