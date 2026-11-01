export const COMMAND_PALETTE_OPEN_EVENT = "ragapp:command-palette-open";

export function dispatchCommandPaletteOpen(): void {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event(COMMAND_PALETTE_OPEN_EVENT));
  }
}
