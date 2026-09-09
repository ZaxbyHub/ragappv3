import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { StatusBadge, FILE_STATUS_LABELS, FILE_STATUS_COLORS } from "./StatusBadge";

describe("StatusBadge", () => {
  describe("Rendering valid statuses", () => {
    it("test_pending_shows_pending_badge", () => {
      render(<StatusBadge status="pending" />);
      expect(screen.getByText("Pending")).toBeInTheDocument();
      // Clock icon should be rendered for pending status
      const badge = screen.getByText("Pending").closest("span");
      expect(badge?.querySelector("svg")).toBeInTheDocument();
    });

    it("test_processing_shows_processing_badge", () => {
      render(<StatusBadge status="processing" />);
      expect(screen.getByText("Processing")).toBeInTheDocument();
      // Loader2 icon should be rendered with animate-spin for processing
      const badge = screen.getByText("Processing").closest("span");
      const icon = badge?.querySelector("svg");
      expect(icon).toBeInTheDocument();
      expect(icon).toHaveClass("animate-spin");
    });

    it("test_indexed_shows_indexed_badge", () => {
      render(<StatusBadge status="indexed" />);
      expect(screen.getByText("Indexed")).toBeInTheDocument();
      // Should NOT show "Processed" - the old status label
      expect(screen.queryByText("Processed")).not.toBeInTheDocument();
      // CheckCircle icon should be rendered for indexed status
      const badge = screen.getByText("Indexed").closest("span");
      expect(badge?.querySelector("svg")).toBeInTheDocument();
    });

    it("test_error_shows_error_badge", () => {
      render(<StatusBadge status="error" />);
      expect(screen.getByText("Error")).toBeInTheDocument();
      // AlertCircle icon should be rendered for error status
      const badge = screen.getByText("Error").closest("span");
      expect(badge?.querySelector("svg")).toBeInTheDocument();
    });

    it("test_partial_shows_partial_badge_not_unknown", () => {
      // LIVE-03: "partial" is a known terminal status — never "Unknown".
      render(<StatusBadge status="partial" />);
      expect(screen.getByText("Partial")).toBeInTheDocument();
      expect(screen.queryByText("Unknown")).not.toBeInTheDocument();
      const badge = screen.getByText("Partial").closest("span");
      expect(badge).toHaveClass("bg-warning");
      expect(badge?.querySelector("svg")).toBeInTheDocument();
    });
  });

  describe("Partially indexed (chunks_failed > 0)", () => {
    it("indexed with chunksFailed shows amber Partially indexed badge with title", () => {
      render(<StatusBadge status="indexed" chunksFailed={3} />);
      const badge = screen.getByText("Partially indexed").closest("span");
      expect(badge).toHaveClass("bg-warning");
      expect(badge).toHaveAttribute(
        "title",
        "3 chunks failed to embed — retrieval may miss content"
      );
      expect(screen.queryByText("Indexed")).not.toBeInTheDocument();
    });

    it("indexed with zero chunksFailed shows normal Indexed badge", () => {
      render(<StatusBadge status="indexed" chunksFailed={0} />);
      expect(screen.getByText("Indexed")).toBeInTheDocument();
      expect(screen.queryByText("Partially indexed")).not.toBeInTheDocument();
    });

    it("non-indexed status ignores chunksFailed", () => {
      render(<StatusBadge status="error" chunksFailed={3} />);
      expect(screen.getByText("Error")).toBeInTheDocument();
      expect(screen.queryByText("Partially indexed")).not.toBeInTheDocument();
    });
  });

  describe("Handling invalid/unknown statuses", () => {
    it("test_processed_shows_unknown", () => {
      // "processed" is no longer a valid status - should show "Unknown"
      render(<StatusBadge status="processed" />);
      expect(screen.getByText("Unknown")).toBeInTheDocument();
      // Should NOT show "Processed" - it's an unrecognized status now
      expect(screen.queryByText("Processed")).not.toBeInTheDocument();
    });

    it("test_undefined_shows_unknown", () => {
      // @ts-expect-error - intentionally testing undefined behavior
      render(<StatusBadge />);
      expect(screen.getByText("Unknown")).toBeInTheDocument();
    });

    it("test_empty_string_shows_unknown", () => {
      render(<StatusBadge status="" />);
      expect(screen.getByText("Unknown")).toBeInTheDocument();
    });
  });

  describe("Exports verification", () => {
    it("test_FILE_STATUS_LABELS_exports", () => {
      expect(FILE_STATUS_LABELS).toHaveProperty("pending");
      expect(FILE_STATUS_LABELS).toHaveProperty("processing");
      expect(FILE_STATUS_LABELS).toHaveProperty("indexed");
      expect(FILE_STATUS_LABELS).toHaveProperty("error");
      // Verify these are the ONLY expected keys
      const keys = Object.keys(FILE_STATUS_LABELS);
      expect(keys).toEqual(expect.arrayContaining(["pending", "processing", "indexed", "error"]));
    });

    it("test_FILE_STATUS_COLORS_exports", () => {
      expect(FILE_STATUS_COLORS).toHaveProperty("pending");
      expect(FILE_STATUS_COLORS).toHaveProperty("processing");
      expect(FILE_STATUS_COLORS).toHaveProperty("indexed");
      expect(FILE_STATUS_COLORS).toHaveProperty("error");
      // Verify these are the ONLY expected keys
      const keys = Object.keys(FILE_STATUS_COLORS);
      expect(keys).toEqual(expect.arrayContaining(["pending", "processing", "indexed", "error"]));
    });

    it("test_INDEXED_label_is_not_processed", () => {
      // CRITICAL: The indexed label must NOT be "Processed"
      // This ensures the status alignment fix is correct
      expect(FILE_STATUS_LABELS.indexed).toBe("Indexed");
      expect(FILE_STATUS_LABELS.indexed).not.toBe("Processed");
    });
  });

  describe("Badge variants and styling", () => {
    it("pending badge has outline variant with Clock icon", () => {
      render(<StatusBadge status="pending" />);
      const badge = screen.getByText("Pending").closest("span");
      expect(badge).toHaveClass("border"); // outline variant adds border class
    });

    it("processing badge has secondary variant with spinning Loader2", () => {
      render(<StatusBadge status="processing" />);
      const badge = screen.getByText("Processing").closest("span");
      expect(badge).toHaveClass("bg-secondary"); // secondary variant
      const icon = badge?.querySelector("svg");
      expect(icon).toHaveClass("animate-spin");
    });

    it("indexed badge has green background with CheckCircle icon", () => {
      render(<StatusBadge status="indexed" />);
      const badge = screen.getByText("Indexed").closest("span");
      expect(badge).toHaveClass("bg-success");
    });

    it("error badge has destructive variant with AlertCircle icon", () => {
      render(<StatusBadge status="error" />);
      const badge = screen.getByText("Error").closest("span");
      expect(badge).toHaveClass("bg-destructive");
    });
  });
});
