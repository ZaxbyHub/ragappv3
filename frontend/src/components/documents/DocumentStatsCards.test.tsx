import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DocumentStatsCards } from "./DocumentStatsCards";
import type { DocumentStatsResponse } from "@/lib/api";

const validStats: DocumentStatsResponse = {
  total_documents: 42,
  total_chunks: 128,
  total_size_bytes: 1048576,
  documents_by_status: { indexed: 35, pending: 5, error: 2 },
};

describe("DocumentStatsCards", () => {
  describe("Renders all 4 stat cards with correct titles", () => {
    it("renders Total Documents card title", () => {
      render(<DocumentStatsCards stats={validStats} />);
      expect(screen.getByText("Total Documents")).toBeInTheDocument();
    });

    it("renders Total Chunks card title", () => {
      render(<DocumentStatsCards stats={validStats} />);
      expect(screen.getByText("Total Chunks")).toBeInTheDocument();
    });

    it("renders Total Size card title", () => {
      render(<DocumentStatsCards stats={validStats} />);
      expect(screen.getByText("Total Size")).toBeInTheDocument();
    });

    it("renders Indexed card title", () => {
      render(<DocumentStatsCards stats={validStats} />);
      expect(screen.getByText("Indexed")).toBeInTheDocument();
    });
  });

  describe("Renders correct stat values", () => {
    it("displays total_documents value", () => {
      render(<DocumentStatsCards stats={validStats} />);
      expect(screen.getByText("42")).toBeInTheDocument();
    });

    it("displays total_chunks value", () => {
      render(<DocumentStatsCards stats={validStats} />);
      expect(screen.getByText("128")).toBeInTheDocument();
    });

    it("displays formatted total_size_bytes value", () => {
      render(<DocumentStatsCards stats={validStats} />);
      // 1048576 bytes = 1.0 MB
      expect(screen.getByText("1.0 MB")).toBeInTheDocument();
    });

    it("displays documents_by_status.indexed value", () => {
      render(<DocumentStatsCards stats={validStats} />);
      expect(screen.getByText("35")).toBeInTheDocument();
    });
  });

  describe("Renders icons with text-muted-foreground class", () => {
    it("renders FileText icon with text-muted-foreground", () => {
      render(<DocumentStatsCards stats={validStats} />);
      const icons = document.querySelectorAll("svg.text-muted-foreground");
      expect(icons.length).toBeGreaterThanOrEqual(4);
    });

    it("renders all 4 icons in separate icon containers", () => {
      render(<DocumentStatsCards stats={validStats} />);
      // Each icon is wrapped in a div with rounded-full bg-muted
      const iconContainers = document.querySelectorAll("div.rounded-full.bg-muted");
      expect(iconContainers.length).toBe(4);
    });
  });

  describe("Handles null/undefined stat values gracefully", () => {
    it("renders with undefined documents_by_status", () => {
      const stats: DocumentStatsResponse = {
        total_documents: 10,
        total_chunks: 20,
        total_size_bytes: 3000,
        // @ts-expect-error — intentionally testing undefined behavior
        documents_by_status: undefined,
      };
      render(<DocumentStatsCards stats={stats} />);
      // Should fall back to 0 for indexed via `|| 0`
      const indexedLabel = screen.getByText("Indexed");
      const indexedValue = indexedLabel.closest("div")?.querySelector("h3");
      expect(indexedValue?.textContent).toBe("0");
    });

    it("renders with null documents_by_status", () => {
      const stats: DocumentStatsResponse = {
        total_documents: 10,
        total_chunks: 20,
        total_size_bytes: 3000,
        documents_by_status: null as unknown as Record<string, number>,
      };
      render(<DocumentStatsCards stats={stats} />);
      // Should fall back to 0 for indexed via `|| 0`
      const indexedLabel = screen.getByText("Indexed");
      const indexedValue = indexedLabel.closest("div")?.querySelector("h3");
      expect(indexedValue?.textContent).toBe("0");
    });

    it("renders with undefined total_size_bytes", () => {
      const stats: DocumentStatsResponse = {
        total_documents: 10,
        total_chunks: 20,
        total_size_bytes: undefined as unknown as number,
        documents_by_status: {},
      };
      render(<DocumentStatsCards stats={stats} />);
      // formatFileSize returns "0 B" for falsy bytes
      expect(screen.getByText("0 B")).toBeInTheDocument();
    });

    it("renders with missing indexed key in documents_by_status", () => {
      const stats: DocumentStatsResponse = {
        total_documents: 10,
        total_chunks: 20,
        total_size_bytes: 5000,
        documents_by_status: { pending: 3 }, // no "indexed" key
      };
      render(<DocumentStatsCards stats={stats} />);
      // Should fall back to 0 via `?.indexed || 0`
      const indexedLabel = screen.getByText("Indexed");
      const indexedValue = indexedLabel.closest("div")?.querySelector("h3");
      expect(indexedValue?.textContent).toBe("0");
    });
  });

  describe("Handles zero values correctly", () => {
    it("renders all zeros without crashing", () => {
      const stats: DocumentStatsResponse = {
        total_documents: 0,
        total_chunks: 0,
        total_size_bytes: 0,
        documents_by_status: { indexed: 0 },
      };
      render(<DocumentStatsCards stats={stats} />);
      // Should display "0 B" from formatFileSize for 0 bytes
      expect(screen.getByText("0 B")).toBeInTheDocument();
      // And 0 for indexed
      expect(screen.getAllByText("0").length).toBeGreaterThanOrEqual(3);
    });

    it("renders zero total_size_bytes as 0 B", () => {
      const stats: DocumentStatsResponse = {
        total_documents: 0,
        total_chunks: 0,
        total_size_bytes: 0,
        documents_by_status: { indexed: 0 },
      };
      render(<DocumentStatsCards stats={stats} />);
      expect(screen.getByText("0 B")).toBeInTheDocument();
    });
  });
});
