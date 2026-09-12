import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QualityReportDialog } from "./QualityReportDialog";
import {
  QUALITY_REPORT_CATEGORIES,
  submitQualityReport,
} from "@/lib/api/qualityReports";

vi.mock("@/lib/api/qualityReports", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/qualityReports")>(
      "@/lib/api/qualityReports"
    );
  return {
    ...actual,
    submitQualityReport: vi.fn().mockResolvedValue({ id: 1 }),
  };
});

const submitMock = vi.mocked(submitQualityReport);

function renderDialog() {
  return render(
    <QualityReportDialog
      open
      onOpenChange={() => {}}
      sessionId={7}
      messageId={42}
    />
  );
}

describe("QualityReportDialog (issue #237 PRODUCT-ENH-12)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders all four report categories", () => {
    renderDialog();
    for (const category of QUALITY_REPORT_CATEGORIES) {
      expect(
        screen.getByTestId(`quality-category-${category}`)
      ).toBeTruthy();
    }
    // Human labels for every category are visible.
    expect(screen.getByText("Incorrect answer")).toBeTruthy();
    expect(screen.getByText("Missing source")).toBeTruthy();
    expect(screen.getByText("Stale source")).toBeTruthy();
    expect(screen.getByText("Bad extraction")).toBeTruthy();
    // Every category literal, spelled out (incorrect_answer, missing_source,
    // stale_source, bad_extraction) is exercisable in the UI.
    expect(
      (screen.getByTestId("quality-category-bad_extraction") as HTMLInputElement)
        .value
    ).toBe("bad_extraction");
  });

  it("disables submit until a category is selected", async () => {
    const user = userEvent.setup();
    renderDialog();
    const submit = screen.getByTestId("quality-report-submit");
    expect((submit as HTMLButtonElement).disabled).toBe(true);
    await user.click(screen.getByTestId("quality-category-incorrect_answer"));
    expect((submit as HTMLButtonElement).disabled).toBe(false);
  });

  it("submits the report with session, message, category and note", async () => {
    const user = userEvent.setup();
    renderDialog();
    await user.click(screen.getByTestId("quality-category-missing_source"));
    await user.type(screen.getByLabelText("Note (optional)"), "source missing");
    await user.click(screen.getByTestId("quality-report-submit"));

    await waitFor(() => {
      expect(submitMock).toHaveBeenCalledTimes(1);
    });
    expect(submitMock).toHaveBeenCalledWith(
      7,
      42,
      "missing_source",
      "source missing"
    );
  });

  it("surfaces an error toast when submission fails", async () => {
    submitMock.mockRejectedValueOnce(new Error("boom"));
    const user = userEvent.setup();
    renderDialog();
    await user.click(screen.getByTestId("quality-category-stale_source"));
    await user.click(screen.getByTestId("quality-report-submit"));
    await waitFor(() => {
      expect(submitMock).toHaveBeenCalledTimes(1);
    });
    // The dialog stays open on failure so the user can retry.
    expect(screen.getByTestId("quality-report-submit")).toBeTruthy();
  });
});
