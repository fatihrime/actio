// Light or dark. The page follows the operating system's setting through CSS; this is
// for the few things CSS can't reach, such as a diagram drawn by a script.
import { useSyncExternalStore } from "react";

const DARK = "(prefers-color-scheme: dark)";

/** Whether the reader's system asks for a dark page. */
export function useDarkScheme(): boolean {
  return useSyncExternalStore(subscribe, isDark, () => false);
}

function isDark(): boolean {
  return watch()?.matches ?? false;
}

function subscribe(changed: () => void): () => void {
  const query = watch();
  query?.addEventListener("change", changed);
  return () => query?.removeEventListener("change", changed);
}

function watch(): MediaQueryList | null {
  // Some browsers that only simulate a page have no such thing.
  return typeof window.matchMedia === "function"
    ? window.matchMedia(DARK)
    : null;
}
