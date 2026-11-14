// What the page can ask about the screen it is on: its width, light or dark. The
// layout follows these through CSS; this is for what CSS can't decide, such as
// whether a panel is open, or the colours of a diagram drawn by a script.
import { useCallback, useSyncExternalStore } from "react";

/** A window at least this wide has room for the list of conversations beside the
 * chat. Tailwind's `md`. */
export const WIDE = "(min-width: 48rem)";

/** Whether a media query holds, kept current. `otherwise` is the answer where nobody
 * can say: while the server prepares the page, and in a browser that only simulates
 * one. */
export function useMedia(query: string, otherwise: boolean): boolean {
  const subscribe = useCallback(
    (changed: () => void) => {
      const list = watch(query);
      list?.addEventListener("change", changed);
      return () => list?.removeEventListener("change", changed);
    },
    [query],
  );
  return useSyncExternalStore(
    subscribe,
    () => watch(query)?.matches ?? otherwise,
    () => otherwise,
  );
}

function watch(query: string): MediaQueryList | null {
  return typeof window.matchMedia === "function"
    ? window.matchMedia(query)
    : null;
}
