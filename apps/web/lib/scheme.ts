// Light or dark. The page follows the operating system's setting through CSS; this is
// for the few things CSS can't reach, such as a diagram drawn by a script.
import { useMedia } from "./media";

/** Whether the reader's system asks for a dark page. */
export function useDarkScheme(): boolean {
  return useMedia("(prefers-color-scheme: dark)", false);
}
