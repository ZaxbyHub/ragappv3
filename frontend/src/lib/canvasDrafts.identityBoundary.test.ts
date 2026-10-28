// Issue #1032 canvas-draft identity-boundary regressions (unfrozen,
// plan-critic round 1 findings 1 and 2). Pins the generation gate and the
// principal-scoped key scheme at the lib level — the frozen C4 check pins
// the boundary wipe through the real login flow.
import { describe, it, expect, beforeEach } from "vitest";
import {
  CANVAS_DRAFT_PREFIX,
  canvasDraftKey,
  clearAllCanvasDrafts,
  clearCanvasDraft,
  currentCanvasDraftGeneration,
  invalidatePendingCanvasDraftWrites,
  readCanvasDraft,
  writeCanvasDraft,
} from "@/lib/canvasDrafts";

function installLocalStorage() {
  const store = new Map<string, string>();
  const impl = {
    getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => {
      // Scriptable refusal: tests set this to make removeItem throw like a
      // degraded storage surface would.
      if (impl.refuseRemoval) throw new DOMException("quota", "QuotaExceededError");
      store.delete(k);
    },
    clear: () => store.clear(),
    key: (i: number) => Array.from(store.keys())[i] ?? null,
    get length() {
      return store.size;
    },
    refuseRemoval: false,
  };
  Object.defineProperty(window, "localStorage", { value: impl, configurable: true });
  return impl;
}

describe("canvasDrafts identity boundary (issue #1032)", () => {
  let storage: ReturnType<typeof installLocalStorage>;

  beforeEach(() => {
    storage = installLocalStorage();
  });

  it("a write scheduled before the generation bump is dropped at execution time", () => {
    const gen = currentCanvasDraftGeneration();
    writeCanvasDraft("art-1", 11, "user A's unsent canvas text", gen);
    expect(readCanvasDraft("art-1", 11)).toBe("user A's unsent canvas text");

    // The identity switch: writers are invalidated, then the keys wiped.
    invalidatePendingCanvasDraftWrites();
    const failures = clearAllCanvasDrafts();
    expect(failures).toBe(0);

    // A trailing debounce/unmount flush carrying the PRE-bump generation
    // (the schedule-time capture) must not re-create the cleared key.
    writeCanvasDraft("art-1", 11, "user A's unsent canvas text (late flush)", gen);
    expect(readCanvasDraft("art-1", 11)).toBeNull();

    // A write captured AFTER the bump (the next principal's) still works.
    writeCanvasDraft(
      "art-1",
      22,
      "user B's fresh canvas text",
      currentCanvasDraftGeneration(),
    );
    expect(readCanvasDraft("art-1", 22)).toBe("user B's fresh canvas text");
  });

  it("keys are principal-scoped: a refused removal leaves residue the next principal cannot read", () => {
    writeCanvasDraft(
      "art-1",
      11,
      "user A's private canvas text",
      currentCanvasDraftGeneration(),
    );
    // The boundary fires; the storage surface refuses the removal.
    invalidatePendingCanvasDraftWrites();
    storage.refuseRemoval = true;
    const failures = clearAllCanvasDrafts();
    expect(failures).toBeGreaterThan(0);

    // The residue exists under A's key but B's read addresses B's key and
    // gets null — the namespace itself is the boundary (chatDrafts
    // precedent), not the best-effort removeItem.
    expect(window.localStorage.getItem(canvasDraftKey("art-1", 11))).toBe(
      "user A's private canvas text",
    );
    expect(readCanvasDraft("art-1", 22)).toBeNull();
    expect(readCanvasDraft("art-1", null)).toBeNull();
  });

  it("clearAllCanvasDrafts sweeps pre-#1032 unscoped residue too", () => {
    window.localStorage.setItem("canvas-draft:art-legacy", "old unscoped draft");
    window.localStorage.setItem("unrelated-key", "keep me");
    const failures = clearAllCanvasDrafts();
    expect(failures).toBe(0);
    expect(window.localStorage.getItem("canvas-draft:art-legacy")).toBeNull();
    expect(window.localStorage.getItem("unrelated-key")).toBe("keep me");
  });

  it("clearAllCanvasDrafts sweeps the principal-scoped keys production writes", () => {
    // Production writers only ever write canvas-draft:<pid>:<uid>; the
    // boundary sweep must remove those exactly like the legacy residue
    // (review-lane finding: the boundary test seeded only legacy shapes).
    writeCanvasDraft("art-1", 11, "A's scoped draft", currentCanvasDraftGeneration());
    writeCanvasDraft("art-2", 11, "A's other draft", currentCanvasDraftGeneration());
    writeCanvasDraft("art-1", 22, "B's scoped draft", currentCanvasDraftGeneration());
    const failures = clearAllCanvasDrafts();
    expect(failures).toBe(0);
    expect(readCanvasDraft("art-1", 11)).toBeNull();
    expect(readCanvasDraft("art-2", 11)).toBeNull();
    expect(readCanvasDraft("art-1", 22)).toBeNull();
  });

  it("clearAllCanvasDrafts returns -1 when the storage enumeration itself fails", () => {
    writeCanvasDraft("art-1", 11, "A's draft", currentCanvasDraftGeneration());
    // Degrade the surface so enumeration (length) throws — the wipe
    // completeness becomes unknown and must NOT read as success (0).
    const broken = {
      get length() {
        throw new DOMException("degraded", "InvalidStateError");
      },
      key: () => null,
      getItem: () => null,
      setItem: () => undefined,
      removeItem: () => undefined,
      clear: () => undefined,
    };
    Object.defineProperty(window, "localStorage", { value: broken, configurable: true });
    expect(clearAllCanvasDrafts()).toBe(-1);
  });

  it("clearCanvasDraft removes only the principal's own key", () => {
    writeCanvasDraft("art-1", 11, "A's text", currentCanvasDraftGeneration());
    writeCanvasDraft("art-1", 22, "B's text", currentCanvasDraftGeneration());
    clearCanvasDraft("art-1", 11);
    expect(readCanvasDraft("art-1", 11)).toBeNull();
    expect(readCanvasDraft("art-1", 22)).toBe("B's text");
  });

  it("anonymous principals share the anon namespace segment", () => {
    writeCanvasDraft("art-1", null, "anon text", currentCanvasDraftGeneration());
    expect(window.localStorage.getItem(`${CANVAS_DRAFT_PREFIX}anon:art-1`)).toBe(
      "anon text",
    );
    expect(readCanvasDraft("art-1", null)).toBe("anon text");
  });
});
