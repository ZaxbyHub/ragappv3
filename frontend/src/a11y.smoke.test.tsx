import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { axe } from "jest-axe";
import { FileText } from "lucide-react";

import { EmptyState } from "@/components/EmptyState";

describe("accessibility smoke gate", () => {
  it("has no axe violations for the empty-state surface", async () => {
    const { container } = render(
      <EmptyState
        icon={FileText}
        title="No documents yet"
        description="Upload files or request write access from an administrator."
        action={{ label: "Open Vaults", onClick: () => {} }}
      />
    );

    const results = await axe(container);
    expect(results.violations).toHaveLength(0);
  });
});
