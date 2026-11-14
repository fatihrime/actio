import { useCallback, useEffect, useRef, useState } from "react";
import type { RefObject } from "react";

/** How close to the end still counts as being at the end, in pixels. */
const NEAR = 48;

/** Keeps the end of a growing conversation in view, until the reader scrolls away
 * from it, and again once they come back to it.
 *
 * An answer grows in more ways than by its words arriving: code is coloured a moment
 * after it is shown, a formula's fonts load, a diagram is drawn. So this watches the
 * size of what is shown, not the messages.
 */
export function useFollow(): {
  /** For the element that scrolls. */
  scroller: RefObject<HTMLDivElement | null>;
  /** For the element inside it that grows. */
  content: RefObject<HTMLDivElement | null>;
  /** Whether the reader has scrolled away from the end, and there is more below. */
  away: boolean;
  /** Go to the end, and stay there. */
  follow: () => void;
} {
  const scroller = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const [away, setAway] = useState(false);
  const following = useRef(true);
  // Where the reader last was, to tell which way a scroll went.
  const top = useRef(0);

  const toEnd = useCallback(() => {
    const scrolling = scroller.current;
    if (scrolling !== null) {
      scrolling.scrollTop = scrolling.scrollHeight;
      top.current = scrolling.scrollTop;
    }
  }, []);

  const follow = useCallback(() => {
    following.current = true;
    setAway(false);
    toEnd();
  }, [toEnd]);

  useEffect(() => {
    const scrolling = scroller.current;
    const growing = content.current;
    if (scrolling === null || growing === null) {
      return;
    }
    const below = () =>
      scrolling.scrollHeight - scrolling.scrollTop - scrolling.clientHeight;

    const scrolled = () => {
      const up = scrolling.scrollTop < top.current;
      const down = scrolling.scrollTop > top.current;
      top.current = scrolling.scrollTop;
      if (up && below() > 1) {
        // The reader went up, however little. Going up while exactly at the end is
        // something else: what is shown got shorter, and the browser moved along.
        following.current = false;
      } else if (down && below() < NEAR) {
        following.current = true;
      }
      setAway(!following.current && below() >= NEAR);
    };

    const resized = () => {
      if (following.current) {
        toEnd();
      } else {
        setAway(below() >= NEAR);
      }
    };

    scrolling.addEventListener("scroll", scrolled, { passive: true });
    const sizes = new ResizeObserver(resized);
    // What is shown grows; the room for it shrinks when the box to write in grows.
    sizes.observe(growing);
    sizes.observe(scrolling);
    return () => {
      scrolling.removeEventListener("scroll", scrolled);
      sizes.disconnect();
    };
  }, [toEnd]);

  return { scroller, content, away, follow };
}
