import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import type { RefObject } from "react";

/** How close to the end still counts as being at the end, in pixels. */
const NEAR = 48;

/** How long after the reader last did something that scrolls they count as still, in
 * milliseconds. Longer than the time between two events of one gesture, of a key held
 * down, or of the frames of a scroll the browser animates itself; shorter than anyone
 * notices (ADR 0015, revision "a long conversation opens newest first"). */
export const STILL_AFTER_MS = 150;

/** The keys that scroll the conversation, wherever the keyboard is. */
const SCROLLING_KEYS = new Set([
  "PageUp",
  "PageDown",
  "Home",
  "End",
  "ArrowUp",
  "ArrowDown",
  " ",
]);

/** Where the reader stands: how far down they are, and the first thing of the
 * conversation that is shown, with how far down that is, in pixels. */
export interface Place {
  top: number;
  first: HTMLElement | null;
  offset: number;
}

/** Keeps the end of a growing conversation in view, until the reader scrolls away
 * from it, and again once they come back to it.
 *
 * An answer grows in more ways than by its words arriving: code is coloured a moment
 * after it is shown, a formula's fonts load, a diagram is drawn. So this watches the
 * size of what is shown, not the messages.
 *
 * Older messages shown above what is shown are the one change that isn't at the end.
 * Whatever shows them waits until the reader is still (`untilStill`), asks where they
 * stand just before (`standing`), and says so just after (`addedAbove`), in the same
 * task: the reader stays at the words they were reading.
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
  /** How long until the reader is still, in milliseconds: 0 when they are. */
  untilStill: () => number;
  /** Where the reader stands, read just before messages are shown above what is
   * shown. Nothing while there is nothing to scroll. */
  standing: () => Place | null;
  /** Messages were just shown above what is shown, and the reader stood at `before`
   * until then: they stay where they were. */
  addedAbove: (before: Place) => void;
} {
  const scroller = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const [away, setAway] = useState(false);
  const following = useRef(true);
  // Where the reader last was, to tell which way a scroll went. The page's own moves
  // put it where they put the scroller, so that they are not taken for the reader's.
  const top = useRef(0);
  // When the reader last did something that scrolls, by the clock.
  const moved = useRef(-Infinity);
  // Where the reader was when what is shown got shorter under them and the browser
  // moved them up to its new end. Should the browser put them back when it gets longer
  // again, that is no scroll of theirs. Nothing once they scroll themselves, up or
  // down, or do something that scrolls (ADR 0015, revision "an answer waits for the
  // model until it has words"). It only matters while the end isn't followed, and
  // whatever takes the reader from the end empties it first.
  const movedFrom = useRef<number | null>(null);

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

  const untilStill = useCallback(() => {
    const since = Date.now() - moved.current;
    // A clock that was put back says nothing of the reader.
    return since < 0 || since >= STILL_AFTER_MS ? 0 : STILL_AFTER_MS - since;
  }, []);

  const standing = useCallback((): Place | null => {
    const scrolling = scroller.current;
    const growing = content.current;
    if (scrolling === null || growing === null) {
      return null;
    }
    const first = growing.querySelector<HTMLElement>(":scope > :not([hidden])");
    return {
      top: scrolling.scrollTop,
      first,
      offset: first?.offsetTop ?? 0,
    };
  }, []);

  const addedAbove = useCallback((before: Place) => {
    const scrolling = scroller.current;
    if (scrolling === null || before.first === null) {
      return;
    }
    // What was added above is how far the first thing that was shown moved down: not
    // how much taller the conversation got, since an answer may have grown at its end
    // in the same change. Where the browser kept the reader's place itself, this puts
    // them where it already did; at the top of what is shown, no browser does.
    const added = before.first.offsetTop - before.offset;
    scrolling.scrollTop = before.top + added;
    // And the note of where the reader last was moves with them. A scroll they made
    // just before, which the browser hasn't told of yet, would otherwise be measured
    // from a place above the one it started at: a scroll up would count as one down,
    // and the end would pull the reader back.
    top.current += added;
    if (movedFrom.current !== null) {
      movedFrom.current += added;
    }
  }, []);

  // At the end before the browser first paints the conversation. The sizes below are
  // watched from an effect, which React runs once the browser has painted what it was
  // given, and are first reported after that: until then the conversation was painted
  // from its top (ADR 0015, revision "a long conversation opens newest first").
  useLayoutEffect(toEnd, [toEnd]);

  useEffect(() => {
    const scrolling = scroller.current;
    const growing = content.current;
    if (scrolling === null || growing === null) {
      return;
    }
    const below = () =>
      scrolling.scrollHeight - scrolling.scrollTop - scrolling.clientHeight;

    const scrolled = () => {
      // Found where the page left it, the scroller was moved by the page itself;
      // anywhere else, by the reader.
      if (Math.abs(scrolling.scrollTop - top.current) >= 1) {
        moved.current = Date.now();
      }
      const up = scrolling.scrollTop < top.current;
      const down = scrolling.scrollTop > top.current;
      const was = top.current;
      top.current = scrolling.scrollTop;
      if (up && below() > 1) {
        // The reader went up, however little. Going up while exactly at the end is
        // something else: what is shown got shorter, and the browser moved along.
        following.current = false;
        movedFrom.current = null;
      } else if (up) {
        // Where the reader was is kept: the first such place, if what is shown gets
        // shorter more than once.
        movedFrom.current ??= was;
      } else if (
        down &&
        movedFrom.current !== null &&
        scrolling.scrollTop < movedFrom.current + 1
      ) {
        // The browser putting the reader back where they were, as far as there is
        // room again, to within a pixel: seen in Chromium a few frames after a wheel
        // was turned, on a busy device. Taken for the reader scrolling down to the
        // end, it had the end followed again, over the wheel they had just turned.
        if (scrolling.scrollTop > movedFrom.current - 1) {
          movedFrom.current = null;
        }
      } else if (down) {
        // The reader's own scroll down. Whatever was kept is behind them now: should
        // what is shown get shorter again, they are moved from where they are.
        movedFrom.current = null;
        if (below() < NEAR) {
          following.current = true;
        }
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

    // The browser scrolls on a thread of its own, and tells the page a frame later.
    // What the reader does to scroll is heard as they do it.
    // The reader does something that scrolls: what moves the scroller next is theirs.
    const theirs = () => {
      moved.current = Date.now();
      movedFrom.current = null;
    };
    const turned = (event: WheelEvent) => {
      theirs();
      if (event.deltaY < 0 && !event.ctrlKey && scrolling.scrollTop > 0) {
        // Up, and there is something above: the end is no longer followed from now
        // on. Followed until the browser says that the reader scrolled, a change of
        // size would take them back to the end, over the scroll under way. A wheel
        // turned with Ctrl held scrolls nothing: it makes the page larger, and so do
        // two fingers moved apart on a trackpad, which a browser reports the same way.
        following.current = false;
      }
    };
    // A finger that moves, or a scrollbar taken hold of.
    const touched = theirs;
    // Those keys scroll the conversation wherever the keyboard is, so they are heard
    // on the window, before anything else on the page has them.
    const pressed = (event: KeyboardEvent) => {
      if (SCROLLING_KEYS.has(event.key)) {
        theirs();
      }
    };

    scrolling.addEventListener("scroll", scrolled, { passive: true });
    scrolling.addEventListener("wheel", turned, { passive: true });
    scrolling.addEventListener("touchmove", touched, { passive: true });
    scrolling.addEventListener("pointerdown", touched, { passive: true });
    window.addEventListener("keydown", pressed, {
      capture: true,
      passive: true,
    });
    const sizes = new ResizeObserver(resized);
    // The page is laid out before its sizes are watched. Safari's engine tells a new
    // observer first what size each element had before it was laid out, nothing by
    // nothing, when the elements came onto the page in that same frame, as they do when
    // the page starts. It then lays them out, finds the scroller changed after it was
    // told of it, and reports "ResizeObserver loop completed with undelivered
    // notifications" as an error of the page: at every page load (ADR 0015, revision
    // "the page is laid out before its sizes are watched").
    scrolling.getBoundingClientRect();
    // What is shown grows; the room for it shrinks when the box to write in grows.
    sizes.observe(growing);
    sizes.observe(scrolling);
    return () => {
      scrolling.removeEventListener("scroll", scrolled);
      scrolling.removeEventListener("wheel", turned);
      scrolling.removeEventListener("touchmove", touched);
      scrolling.removeEventListener("pointerdown", touched);
      window.removeEventListener("keydown", pressed, { capture: true });
      sizes.disconnect();
    };
  }, [toEnd]);

  return {
    scroller,
    content,
    away,
    follow,
    untilStill,
    standing,
    addedAbove,
  };
}
