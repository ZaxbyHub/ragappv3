import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { KMSCards, KMSCard } from "./KMSCards";
import type { KMSReference } from "@/lib/api";

const makeKms = (overrides: Partial<KMSReference> = {}): KMSReference => ({
  kms_label: "K1",
  entry_id: 7,
  slug: "onboarding-guide",
  title: "Onboarding Guide",
  summary: "How to onboard new members.",
  excerpt: "How to onboard new members and set up access.",
  tags: ["howto", "access"],
  status: "published",
  source_type: "document",
  file_id: 3,
  score: 0.7,
  score_type: "kms_fts",
  ...overrides,
});

describe("KMSCards", () => {
  it("renders nothing when kmsRefs is empty", () => {
    const { container } = render(<KMSCards kmsRefs={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing when kmsRefs is null-cast", () => {
    const { container } = render(<KMSCards kmsRefs={null as unknown as KMSReference[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders kms-cards container with header", () => {
    render(<KMSCards kmsRefs={[makeKms()]} />);
    expect(screen.getByTestId("kms-cards")).toBeInTheDocument();
    expect(screen.getByText("Knowledge base:")).toBeInTheDocument();
  });

  it("renders a card per ref", () => {
    render(
      <KMSCards
        kmsRefs={[
          makeKms({ kms_label: "K1", title: "Entry One" }),
          makeKms({ kms_label: "K2", title: "Entry Two" }),
        ]}
      />
    );
    expect(screen.getByLabelText("Knowledge K1: Entry One")).toBeInTheDocument();
    expect(screen.getByLabelText("Knowledge K2: Entry Two")).toBeInTheDocument();
  });
});

describe("KMSCard", () => {
  it("renders the label badge and tags", () => {
    render(<KMSCard kmsRef={makeKms({ kms_label: "K3" })} />);
    expect(screen.getByLabelText("Knowledge label K3")).toBeInTheDocument();
    expect(screen.getByText("howto")).toBeInTheDocument();
  });

  it("expands long body content on toggle", () => {
    const long = "x".repeat(300);
    render(<KMSCard kmsRef={makeKms({ excerpt: long, summary: long })} />);
    const moreBtn = screen.getByRole("button", { name: /more/i });
    expect(moreBtn).toBeInTheDocument();
    fireEvent.click(moreBtn);
    expect(screen.getByRole("button", { name: /less/i })).toBeInTheDocument();
  });
});
