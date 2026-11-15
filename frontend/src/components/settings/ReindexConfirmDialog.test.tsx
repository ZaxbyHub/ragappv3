import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ReindexConfirmDialog } from "./ReindexConfirmDialog";

describe("ReindexConfirmDialog", () => {
  function setup(overrides: Partial<Parameters<typeof ReindexConfirmDialog>[0]> = {}) {
    const props = {
      open: true,
      onOpenChange: vi.fn(),
      dirtyReindexFields: ["embedding_model"],
      onConfirm: vi.fn(),
      saving: false,
      ...overrides,
    };
    render(<ReindexConfirmDialog {...props} />);
    return props;
  }

  it("renders the title and the list of affected fields", () => {
    setup({
      dirtyReindexFields: ["embedding_model", "chunk_size_chars"],
    });
    expect(screen.getByText(/Re-index required/i)).toBeInTheDocument();
    expect(screen.getByText("embedding_model")).toBeInTheDocument();
    expect(screen.getByText("chunk_size_chars")).toBeInTheDocument();
  });

  it("calls onConfirm when the Save button is clicked", () => {
    const props = setup();
    fireEvent.click(screen.getByRole("button", { name: /Save and acknowledge/i }));
    expect(props.onConfirm).toHaveBeenCalledOnce();
  });

  it("calls onOpenChange(false) when Cancel is clicked", () => {
    const props = setup();
    fireEvent.click(screen.getByRole("button", { name: /Cancel/i }));
    expect(props.onOpenChange).toHaveBeenCalledWith(false);
  });

  it("disables both buttons while saving", () => {
    setup({ saving: true });
    expect(screen.getByRole("button", { name: /Saving/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /Cancel/i })).toBeDisabled();
  });

  it("renders nothing when open is false", () => {
    setup({ open: false });
    expect(screen.queryByText(/Re-index required/i)).not.toBeInTheDocument();
  });

  it("renders 'a field' singular when one field is dirty", () => {
    setup({ dirtyReindexFields: ["embedding_model"] });
    expect(
      screen.getByText(/about to save changes to a field/i),
    ).toBeInTheDocument();
  });

  it("renders 'fields' plural when multiple are dirty", () => {
    setup({ dirtyReindexFields: ["embedding_model", "vector_metric"] });
    expect(
      screen.getByText(/about to save changes to fields/i),
    ).toBeInTheDocument();
  });

  it("states the gated recovery contract and drops the pre-#695 false claims (issue #833)", () => {
    setup({ dirtyReindexFields: ["embedding_model"] });
    const body = screen.getByText(/embedding-mismatch 503/i, {
      exact: false,
    });
    expect(body.textContent).toMatch(/all-vault reindex/i);
    expect(body.textContent).toMatch(/run to completion/i);
    expect(body.textContent).toMatch(/restarting does not clear/i);
    // The pre-#695 claims are false once the readiness gate flips on save:
    // embeddings are NOT queryable and new uploads do NOT go through until
    // the all-vault reindex completes.
    expect(body.textContent).not.toMatch(/remain queryable/i);
    expect(body.textContent).not.toMatch(
      /new documents will use the new settings immediately/i,
    );
  });
});
