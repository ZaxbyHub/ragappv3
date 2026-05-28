import { describe, it, expect, beforeEach, vi } from "vitest";

// Mock window.innerWidth for isMobile() function
const mockWindowInnerWidth = (width: number) => {
  Object.defineProperty(window, "innerWidth", {
    writable: true,
    configurable: true,
    value: width,
  });
};

describe("useChatShellStore", () => {
  beforeEach(() => {
    // Reset window.innerWidth to desktop width before each test
    mockWindowInnerWidth(1024);
    // Clear module cache to get fresh store instance
    vi.resetModules();
  });

  describe("test_initial_state_desktop", () => {
    it("should have sessionRailOpen=true, rightPaneOpen=false, rightPaneWidth=400, activeSessionId=null on desktop", async () => {
      // Desktop width (>=768)
      mockWindowInnerWidth(1024);

      // Import fresh store after mocking window
      const { useChatShellStore } = await import("./useChatShellStore");

      const state = useChatShellStore.getState();

      expect(state.sessionRailOpen).toBe(true);
      expect(state.rightPaneOpen).toBe(false);
      expect(state.rightPaneWidth).toBe(400);
      expect(state.activeSessionId).toBe(null);
    });
  });

  describe("test_toggle_session_rail", () => {
    it("toggleSessionRail() should flip sessionRailOpen", async () => {
      const { useChatShellStore } = await import("./useChatShellStore");
      
      const initialState = useChatShellStore.getState().sessionRailOpen;
      useChatShellStore.getState().toggleSessionRail();
      expect(useChatShellStore.getState().sessionRailOpen).toBe(!initialState);
      
      // Toggle again to verify it flips back
      useChatShellStore.getState().toggleSessionRail();
      expect(useChatShellStore.getState().sessionRailOpen).toBe(initialState);
    });
  });

  describe("test_toggle_right_pane", () => {
    it("toggleRightPane() should flip rightPaneOpen", async () => {
      const { useChatShellStore } = await import("./useChatShellStore");
      
      const initialState = useChatShellStore.getState().rightPaneOpen;
      useChatShellStore.getState().toggleRightPane();
      expect(useChatShellStore.getState().rightPaneOpen).toBe(!initialState);
      
      // Toggle again to verify it flips back
      useChatShellStore.getState().toggleRightPane();
      expect(useChatShellStore.getState().rightPaneOpen).toBe(initialState);
    });
  });

  describe("test_set_right_pane_width", () => {
    it("setRightPaneWidth(400) should set width to 400", async () => {
      const { useChatShellStore } = await import("./useChatShellStore");
      
      useChatShellStore.getState().setRightPaneWidth(400);
      expect(useChatShellStore.getState().rightPaneWidth).toBe(400);
    });
  });

  describe("test_right_pane_width_min_clamp", () => {
    it("setRightPaneWidth(100) should clamp to 320 (min)", async () => {
      const { useChatShellStore } = await import("./useChatShellStore");

      useChatShellStore.getState().setRightPaneWidth(100);
      expect(useChatShellStore.getState().rightPaneWidth).toBe(320);
    });
  });

  describe("test_right_pane_width_max_clamp", () => {
    it("setRightPaneWidth(800) should clamp to 600 (max)", async () => {
      const { useChatShellStore } = await import("./useChatShellStore");
      
      useChatShellStore.getState().setRightPaneWidth(800);
      expect(useChatShellStore.getState().rightPaneWidth).toBe(600);
    });
  });

  describe("test_set_active_session_id", () => {
    it('setActiveSessionId("abc") should set activeSessionId to "abc"', async () => {
      const { useChatShellStore } = await import("./useChatShellStore");
      
      useChatShellStore.getState().setActiveSessionId("abc");
      expect(useChatShellStore.getState().activeSessionId).toBe("abc");
    });
  });

  describe("test_clear_active_session_id", () => {
    it("setActiveSessionId(null) should set activeSessionId to null", async () => {
      const { useChatShellStore } = await import("./useChatShellStore");
      
      // First set a session ID
      useChatShellStore.getState().setActiveSessionId("test-session");
      expect(useChatShellStore.getState().activeSessionId).toBe("test-session");
      
      // Now clear it
      useChatShellStore.getState().setActiveSessionId(null);
      expect(useChatShellStore.getState().activeSessionId).toBe(null);
    });
  });

  describe("test_open_session_rail", () => {
    it("openSessionRail() should set sessionRailOpen to true", async () => {
      const { useChatShellStore } = await import("./useChatShellStore");
      
      // First close it
      useChatShellStore.getState().closeSessionRail();
      expect(useChatShellStore.getState().sessionRailOpen).toBe(false);
      
      // Now open it
      useChatShellStore.getState().openSessionRail();
      expect(useChatShellStore.getState().sessionRailOpen).toBe(true);
    });
  });

  describe("test_close_session_rail", () => {
    it("closeSessionRail() should set sessionRailOpen to false", async () => {
      const { useChatShellStore } = await import("./useChatShellStore");
      
      // First open it
      useChatShellStore.getState().openSessionRail();
      expect(useChatShellStore.getState().sessionRailOpen).toBe(true);
      
      // Now close it
      useChatShellStore.getState().closeSessionRail();
      expect(useChatShellStore.getState().sessionRailOpen).toBe(false);
    });
  });

  describe("test_open_right_pane", () => {
    it("openRightPane() should set rightPaneOpen to true", async () => {
      const { useChatShellStore } = await import("./useChatShellStore");
      
      // First close it
      useChatShellStore.getState().closeRightPane();
      expect(useChatShellStore.getState().rightPaneOpen).toBe(false);
      
      // Now open it
      useChatShellStore.getState().openRightPane();
      expect(useChatShellStore.getState().rightPaneOpen).toBe(true);
    });
  });

  describe("test_close_right_pane", () => {
    it("closeRightPane() should set rightPaneOpen to false", async () => {
      const { useChatShellStore } = await import("./useChatShellStore");
      
      // First open it
      useChatShellStore.getState().openRightPane();
      expect(useChatShellStore.getState().rightPaneOpen).toBe(true);
      
      // Now close it
      useChatShellStore.getState().closeRightPane();
      expect(useChatShellStore.getState().rightPaneOpen).toBe(false);
    });
  });

  describe("test_mobile_initial_state", () => {
    it("should have sessionRailOpen=false on mobile devices", async () => {
      // Mobile width (<768)
      mockWindowInnerWidth(375);
      
      // Import fresh store after mocking window
      const { useChatShellStore } = await import("./useChatShellStore");
      
      const state = useChatShellStore.getState();
      
      expect(state.sessionRailOpen).toBe(false);
      expect(state.rightPaneOpen).toBe(false);
      expect(state.rightPaneWidth).toBe(400);
      expect(state.activeSessionId).toBe(null);
    });
  });

  describe("test_boundary_values", () => {
    it("should accept exactly min width 320", async () => {
      const { useChatShellStore } = await import("./useChatShellStore");

      useChatShellStore.getState().setRightPaneWidth(320);
      expect(useChatShellStore.getState().rightPaneWidth).toBe(320);
    });

    it("should accept exactly max width 600", async () => {
      const { useChatShellStore } = await import("./useChatShellStore");
      
      useChatShellStore.getState().setRightPaneWidth(600);
      expect(useChatShellStore.getState().rightPaneWidth).toBe(600);
    });

    it("should accept width between min and max", async () => {
      const { useChatShellStore } = await import("./useChatShellStore");
      
      useChatShellStore.getState().setRightPaneWidth(400);
      expect(useChatShellStore.getState().rightPaneWidth).toBe(400);
    });
  });
});
