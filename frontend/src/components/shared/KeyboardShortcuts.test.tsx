import { render, screen, fireEvent, act } from "@testing-library/react";
import { renderHook } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { useKeyboardShortcuts, KeyboardShortcutsDialog } from "./KeyboardShortcuts";

describe("KeyboardShortcutsDialog", () => {
  const noop = () => {};

  describe("Accessibility fix — DialogDescription", () => {
    it("renders DialogDescription when dialog is open", () => {
      render(<KeyboardShortcutsDialog open={true} onOpenChange={noop} />);
      expect(screen.getByRole("dialog")).toBeInTheDocument();
      const description = screen.getByText("Available keyboard shortcuts for quick navigation");
      expect(description).toBeInTheDocument();
    });

    it("DialogDescription has correct text content", () => {
      render(<KeyboardShortcutsDialog open={true} onOpenChange={noop} />);
      // Radix DialogDescription renders as a <p> element accessible via description role
      const description = screen.getByText("Available keyboard shortcuts for quick navigation");
      expect(description.tagName).toBe("P");
    });

    it("does NOT render dialog content when open is false", () => {
      render(<KeyboardShortcutsDialog open={false} onOpenChange={noop} />);
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(screen.queryByText("Available keyboard shortcuts for quick navigation")).not.toBeInTheDocument();
    });
  });

  describe("DialogTitle still present", () => {
    it("renders DialogTitle 'Keyboard Shortcuts' when open", () => {
      render(<KeyboardShortcutsDialog open={true} onOpenChange={noop} />);
      const title = screen.getByText("Keyboard Shortcuts");
      expect(title).toBeInTheDocument();
    });

    it("DialogTitle is an h2 element (Radix convention)", () => {
      render(<KeyboardShortcutsDialog open={true} onOpenChange={noop} />);
      const title = screen.getByText("Keyboard Shortcuts");
      expect(title.tagName).toBe("H2");
    });
  });

  describe("Shortcuts list rendering", () => {
    it("renders all 5 keyboard shortcuts", () => {
      render(<KeyboardShortcutsDialog open={true} onOpenChange={noop} />);
      expect(screen.getByText("Enter")).toBeInTheDocument();
      expect(screen.getByText("Send message")).toBeInTheDocument();
      expect(screen.getByText("Shift + Enter")).toBeInTheDocument();
      expect(screen.getByText("New line in message")).toBeInTheDocument();
      expect(screen.getByText("Ctrl/Cmd + Enter")).toBeInTheDocument();
      expect(screen.getByText("Send message (alternative)")).toBeInTheDocument();
      expect(screen.getByText("?")).toBeInTheDocument();
      expect(screen.getByText("Show keyboard shortcuts")).toBeInTheDocument();
      expect(screen.getByText("Esc")).toBeInTheDocument();
      expect(screen.getByText("Close dialogs / Stop streaming")).toBeInTheDocument();
    });

    it("renders shortcut keys with mono font styling", () => {
      render(<KeyboardShortcutsDialog open={true} onOpenChange={noop} />);
      const enterKey = screen.getByText("Enter");
      expect(enterKey).toHaveClass("font-mono");
    });
  });

  describe("Dialog close interaction", () => {
    it("calls onOpenChange(false) when close button is clicked", () => {
      const onOpenChange = vi.fn();
      render(<KeyboardShortcutsDialog open={true} onOpenChange={onOpenChange} />);
      const closeButton = screen.getByRole("button", { name: /close/i });
      fireEvent.click(closeButton);
      expect(onOpenChange).toHaveBeenCalledWith(false);
    });
  });
});

describe("useKeyboardShortcuts", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("initializes with open=false", () => {
    const { result } = renderHook(() => useKeyboardShortcuts());
    expect(result.current.open).toBe(false);
  });

  it("sets open=true when '?' key is pressed outside input", () => {
    const { result } = renderHook(() => useKeyboardShortcuts());
    expect(result.current.open).toBe(false);

    act(() => {
      fireEvent.keyDown(window, { key: "?", shiftKey: false, ctrlKey: false, metaKey: false });
    });

    expect(result.current.open).toBe(true);
  });

  it("opens with Shift+? (the physical US-layout keypress)", () => {
    // Typing "?" on a US layout is Shift + the "/" key, so the handler must
    // not exclude shiftKey (the old !shiftKey guard made the shortcut
    // unopenable on real keyboards).
    const { result } = renderHook(() => useKeyboardShortcuts());
    expect(result.current.open).toBe(false);

    act(() => {
      fireEvent.keyDown(window, { key: "?", shiftKey: true, ctrlKey: false, metaKey: false });
    });

    expect(result.current.open).toBe(true);
  });

  it("does NOT open when '?' is pressed with ctrl+shift (browser shortcut chord)", () => {
    const { result } = renderHook(() => useKeyboardShortcuts());
    expect(result.current.open).toBe(false);

    act(() => {
      fireEvent.keyDown(window, { key: "?", shiftKey: true, ctrlKey: true, metaKey: false });
    });

    expect(result.current.open).toBe(false);
  });

  it("does NOT open when '?' is pressed with meta (Cmd) held", () => {
    const { result } = renderHook(() => useKeyboardShortcuts());
    expect(result.current.open).toBe(false);

    act(() => {
      fireEvent.keyDown(window, { key: "?", shiftKey: true, ctrlKey: false, metaKey: true });
    });

    expect(result.current.open).toBe(false);
  });

  it("does NOT open when '?' is pressed inside an INPUT element", () => {
    const { result } = renderHook(() => useKeyboardShortcuts());
    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();

    act(() => {
      fireEvent.keyDown(input, { key: "?", shiftKey: false, ctrlKey: false, metaKey: false });
    });

    expect(result.current.open).toBe(false);
    document.body.removeChild(input);
  });

  it("does NOT open when '?' is pressed inside a TEXTAREA element", () => {
    const { result } = renderHook(() => useKeyboardShortcuts());
    const textarea = document.createElement("textarea");
    document.body.appendChild(textarea);
    textarea.focus();

    act(() => {
      fireEvent.keyDown(textarea, { key: "?", shiftKey: false, ctrlKey: false, metaKey: false });
    });

    expect(result.current.open).toBe(false);
    document.body.removeChild(textarea);
  });

  it("returns setOpen for external control", () => {
    const { result } = renderHook(() => useKeyboardShortcuts());
    expect(typeof result.current.setOpen).toBe("function");

    act(() => {
      result.current.setOpen(true);
    });
    expect(result.current.open).toBe(true);

    act(() => {
      result.current.setOpen(false);
    });
    expect(result.current.open).toBe(false);
  });

  it("cleans up event listener on unmount", () => {
    const removeSpy = vi.spyOn(window, "removeEventListener");
    const { unmount } = renderHook(() => useKeyboardShortcuts());
    unmount();
    expect(removeSpy).toHaveBeenCalledWith("keydown", expect.any(Function));
  });
});
