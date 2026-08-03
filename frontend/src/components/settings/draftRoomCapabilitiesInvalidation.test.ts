import { describe, it, expect } from "vitest";
import { payloadTogglesDraftRoom } from "./draftRoomCapabilitiesInvalidation";

describe("payloadTogglesDraftRoom", () => {
  it("returns true when draft_room_enabled is present in the dirty payload", () => {
    expect(payloadTogglesDraftRoom({ draft_room_enabled: true })).toBe(true);
    expect(payloadTogglesDraftRoom({ draft_room_enabled: false })).toBe(true);
  });

  it("returns false when draft_room_enabled is absent", () => {
    expect(payloadTogglesDraftRoom({ chunk_size_chars: 1500 })).toBe(false);
    expect(payloadTogglesDraftRoom({})).toBe(false);
  });
});
