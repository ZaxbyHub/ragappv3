import { describe, it, expect, vi, beforeEach } from "vitest";
import { render as rtlRender, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { WikiCards, WikiCard } from "./WikiCards";
import type { WikiReference } from "@/lib/api";

// WikiCard calls useNavigate() for in-app navigation; spy on it to assert routing.
const navigateMock = vi.hoisted(() => vi.fn());
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>(
    "react-router-dom"
  );
  return { ...actual, useNavigate: () => navigateMock };
});

// Components calling router hooks need a Router context (see frontend-testing-gotchas).
const render: typeof rtlRender = (ui, options) =>
  rtlRender(ui, { wrapper: MemoryRouter, ...options });

beforeEach(() => {
  navigateMock.mockClear();
});

const makeWiki = (overrides: Partial<WikiReference> = {}): WikiReference => ({
  wiki_label: "W1",
  page_id: 1,
  claim_id: 10,
  title: "Task Force Alpha",
  slug: "task-force-alpha",
  page_type: "entity",
  claim_text: "Task Force Alpha is responsible for regional operations.",
  excerpt: null,
  confidence: 0.9,
  status: "active",
  page_status: "verified",
  claim_status: "active",
  score: 0.85,
  score_type: "fts",
  source_count: 3,
  provenance_summary: "3 documents",
  ...overrides,
});

describe("WikiCards", () => {
  it("renders nothing when wikiRefs is empty", () => {
    const { container } = render(<WikiCards wikiRefs={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing when wikiRefs is undefined-like (null cast)", () => {
    const { container } = render(<WikiCards wikiRefs={null as unknown as WikiReference[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders wiki-cards container with entries", () => {
    render(<WikiCards wikiRefs={[makeWiki()]} />);
    expect(screen.getByTestId("wiki-cards")).toBeInTheDocument();
    expect(screen.getByText("Wiki knowledge:")).toBeInTheDocument();
  });

  it("renders a card for each wiki ref", () => {
    render(
      <WikiCards
        wikiRefs={[
          makeWiki({ wiki_label: "W1", title: "Page One" }),
          makeWiki({ wiki_label: "W2", title: "Page Two" }),
        ]}
      />
    );
    expect(screen.getByLabelText("Wiki W1: Page One")).toBeInTheDocument();
    expect(screen.getByLabelText("Wiki W2: Page Two")).toBeInTheDocument();
  });
});

describe("WikiCard", () => {
  it("renders wiki label badge", () => {
    render(<WikiCard wikiRef={makeWiki({ wiki_label: "W3" })} />);
    expect(screen.getByLabelText("Wiki label W3")).toBeInTheDocument();
  });

  it("renders title", () => {
    render(<WikiCard wikiRef={makeWiki({ title: "AFOMIS Overview" })} />);
    expect(screen.getByText("AFOMIS Overview")).toBeInTheDocument();
  });

  it("renders page_type", () => {
    render(<WikiCard wikiRef={makeWiki({ page_type: "process" })} />);
    expect(screen.getByText("process")).toBeInTheDocument();
  });

  it("renders confidence percentage", () => {
    render(<WikiCard wikiRef={makeWiki({ confidence: 0.87 })} />);
    expect(screen.getByText("87%")).toBeInTheDocument();
  });

  it("renders claim_status when present", () => {
    render(<WikiCard wikiRef={makeWiki({ claim_status: "verified", page_status: "draft" })} />);
    expect(screen.getByText("verified")).toBeInTheDocument();
  });

  it("falls back to page_status when claim_status is null", () => {
    render(<WikiCard wikiRef={makeWiki({ claim_status: null, page_status: "stale" })} />);
    expect(screen.getByText("stale")).toBeInTheDocument();
  });

  it("renders claim_text as body", () => {
    render(<WikiCard wikiRef={makeWiki({ claim_text: "The chief is Col Smith." })} />);
    expect(screen.getByText("The chief is Col Smith.")).toBeInTheDocument();
  });

  it("falls back to excerpt when claim_text is null", () => {
    render(<WikiCard wikiRef={makeWiki({ claim_text: null, excerpt: "From the excerpt." })} />);
    expect(screen.getByText("From the excerpt.")).toBeInTheDocument();
  });

  it("renders provenance_summary", () => {
    render(<WikiCard wikiRef={makeWiki({ provenance_summary: "5 documents" })} />);
    expect(screen.getByText("5 documents")).toBeInTheDocument();
  });

  it("renders external link button when slug is present", () => {
    render(<WikiCard wikiRef={makeWiki({ slug: "task-force-alpha" })} />);
    expect(screen.getByLabelText("Open wiki page Task Force Alpha")).toBeInTheDocument();
  });

  it("does not render external link button when page_id and slug are both null", () => {
    render(<WikiCard wikiRef={makeWiki({ page_id: null, slug: null })} />);
    expect(screen.queryByRole("button", { name: /open wiki page/i })).not.toBeInTheDocument();
  });

  it("navigates to the wiki page by id when the open link is clicked", () => {
    render(<WikiCard wikiRef={makeWiki({ page_id: 1, slug: "task-force-alpha" })} />);
    fireEvent.click(screen.getByLabelText("Open wiki page Task Force Alpha"));
    expect(navigateMock).toHaveBeenCalledWith("/wiki?page=1");
  });

  it("navigates by slug when page_id is null", () => {
    render(<WikiCard wikiRef={makeWiki({ page_id: null, slug: "task-force-alpha" })} />);
    fireEvent.click(screen.getByLabelText("Open wiki page Task Force Alpha"));
    expect(navigateMock).toHaveBeenCalledWith("/wiki?page=task-force-alpha");
  });

  it("truncates long body and shows More button", () => {
    const longText = "A".repeat(200);
    render(<WikiCard wikiRef={makeWiki({ claim_text: longText })} />);
    expect(screen.getByRole("button", { name: /more/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /less/i })).not.toBeInTheDocument();
  });

  it("expands long body on More click", () => {
    const longText = "A".repeat(200);
    render(<WikiCard wikiRef={makeWiki({ claim_text: longText })} />);
    fireEvent.click(screen.getByRole("button", { name: /more/i }));
    expect(screen.getByRole("button", { name: /less/i })).toBeInTheDocument();
    expect(screen.getByText(longText)).toBeInTheDocument();
  });

  it("does not show More button for short body", () => {
    render(<WikiCard wikiRef={makeWiki({ claim_text: "Short." })} />);
    expect(screen.queryByRole("button", { name: /more/i })).not.toBeInTheDocument();
  });
});
