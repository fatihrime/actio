// The page's frames, for a test that says when the next one comes. The simulated
// browser draws frames by itself, about sixty a second (jsdom's `pretendToBeVisual`,
// which Vitest turns on), and runs at each what was asked of it.
import { vi } from "vitest";

/** From now on, a frame comes only when the test calls what this hands back: it runs
 * every callback asked for since the frame before (`requestAnimationFrame`), in one
 * go, as a browser does before it paints. Call it inside `act` when the page is
 * rendered. The browser's own frames are back for the next test (`unstubGlobals` in
 * vitest.config.mts). */
export function holdFrames(): () => void {
  let asked = new Map<number, FrameRequestCallback>();
  let last = 0;
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    last += 1;
    asked.set(last, callback);
    return last;
  });
  vi.stubGlobal("cancelAnimationFrame", (handle: number) => {
    asked.delete(handle);
  });
  return () => {
    const now = asked;
    asked = new Map();
    for (const callback of now.values()) {
      callback(performance.now());
    }
  };
}
