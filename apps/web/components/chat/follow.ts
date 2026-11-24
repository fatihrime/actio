import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import type { RefObject } from "react";

import { PATH_LONGEST } from "@/lib/chat/places";
import type { Spot } from "@/lib/chat/places";

/** How close to the end still counts as being at the end, in pixels. */
const NEAR = 48;

/** The messages that are shown in the element that grows, from the top: those of the
 * steps that are shown, and those after them. A message is found by its id
 * (`data-message-id` on its article, message.tsx). */
const SHOWN_MESSAGES =
  ":scope > [data-message-id], :scope > :not([hidden]) > [data-message-id]";

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

/** `element`'s box, when it is drawn: nothing when it has no height, or the browser
 * says it isn't drawn, as what a folded thought holds may be laid out and not drawn. */
function drawnBox(element: Element): DOMRect | null {
  const box = element.getBoundingClientRect();
  return box.height === 0 ||
    (typeof element.checkVisibility === "function" &&
      !element.checkVisibility())
    ? null
    : box;
}

/** The index of the first of `children` that is drawn and whose bottom edge is below
 * `edge`, or nothing when none is. Those that are drawn stand one under another, so it
 * is found by halves: a few reads of the layout, however many blocks an answer has. One
 * that isn't drawn is no answer, and in the search stands for the next one after it
 * that is. */
function firstAcross(children: HTMLCollection, edge: number): number | null {
  // The first child from `index` on that is drawn, with its box.
  const drawnFrom = (index: number) => {
    for (let at = index; at < children.length; at++) {
      const child = children[at];
      const box = child === undefined ? null : drawnBox(child);
      if (box !== null) {
        return { at, box };
      }
    }
    return null;
  };
  let low = 0;
  let high = children.length;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    const drawn = drawnFrom(middle);
    if (drawn === null || drawn.box.bottom > edge) {
      high = middle;
    } else {
      low = drawn.at + 1;
    }
  }
  return drawnFrom(low)?.at ?? null;
}

/** Where the reader is in `growing`, which `scrolling` scrolls: the first message shown
 * whose bottom edge is below the top edge of `scrolling`, the block of it that crosses
 * that edge, and how far the edge is below the block's top edge. Nothing when no
 * message is. */
function spotIn(scrolling: HTMLElement, growing: HTMLElement): Spot | null {
  const messages = growing.querySelectorAll<HTMLElement>(SHOWN_MESSAGES);
  const edge = scrolling.getBoundingClientRect().top;
  // They stand one under another, so that message is found by halves: a few reads of
  // the layout, however long the conversation.
  let low = 0;
  let high = messages.length;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    const bottom = messages[middle]?.getBoundingClientRect().bottom ?? edge;
    if (bottom > edge) {
      high = middle;
    } else {
      low = middle + 1;
    }
  }
  const found = messages[low];
  const id = found?.dataset.messageId;
  if (found === undefined || id === undefined) {
    return null;
  }
  // Down from the message, at each level to the first child that is drawn and whose
  // bottom edge is below the window's top edge: what is in view.
  const path: number[] = [];
  let block: Element = found;
  for (let level = 0; level < PATH_LONGEST; level++) {
    const index = firstAcross(block.children, edge);
    const child = index === null ? undefined : block.children[index];
    if (index === null || child === undefined) {
      break;
    }
    path.push(index);
    block = child;
  }
  return {
    message: id,
    path,
    offset: edge - block.getBoundingClientRect().top,
  };
}

/** Put the reader at `spot` in `growing`, which `scrolling` scrolls: the window's top
 * edge at the place's block, down the place's path from its message as far as the path
 * leads. It leads no further where the message is drawn otherwise now, as when it is
 * shown as written, and where what it leads to isn't drawn now, as in a thought that
 * was open and is folded: the window's top edge goes into the deepest element found
 * that is drawn, but no further than its end. Says how far short of the place the
 * reader is then, in pixels, when the end of what is shown held them back: 0 at the
 * place. Nothing when the place's message isn't shown, and then nothing moved. */
function goTo(
  scrolling: HTMLElement,
  growing: HTMLElement,
  spot: Spot,
): number | null {
  const message = [
    ...growing.querySelectorAll<HTMLElement>(SHOWN_MESSAGES),
  ].find((shown) => shown.dataset.messageId === spot.message);
  if (message === undefined) {
    return null;
  }
  let block: Element = message;
  let whole = true;
  for (const index of spot.path) {
    const child = block.children[index];
    if (child === undefined || drawnBox(child) === null) {
      whole = false;
      break;
    }
    block = child;
  }
  const offset = whole
    ? spot.offset
    : Math.min(spot.offset, block.getBoundingClientRect().height);
  const at = () =>
    scrolling.getBoundingClientRect().top - block.getBoundingClientRect().top;
  scrolling.scrollTop += offset - at();
  return Math.max(0, offset - at());
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
 *
 * A conversation that its reader left away from its end opens where they left it
 * (`at`), and its end isn't followed. Where the reader is, the hook says (`remember`):
 * nothing while the end is followed, and otherwise their place, read after they
 * scrolled, at most once a frame, and when the conversation leaves the page. While
 * messages it came with are still held back below what is shown (`heldBelow`), the end
 * of what is shown isn't the conversation's: a reader who scrolls down to it isn't
 * taken to follow the end, and is offered the way to the newest message.
 *
 * Before the browser paints, the window is filled, or nothing is left to fill it with.
 * When what is shown holds the reader short of their place as the conversation opens,
 * the next of the messages held back below are shown at once (`showBelow`); when it is
 * less than the window is high, those below, or else those above (`showAbove`). And so
 * while the end is followed, as when it opens at its end or the reader asks for it,
 * with those above.
 */
export function useFollow({
  at = null,
  heldBelow = false,
  heldAbove = false,
  remember,
  showBelow,
  showAbove,
}: {
  /** Where the reader left the conversation, when they left it away from its end: it
   * opens there, and not at its end. Read when the hook starts. */
  at?: Spot | null;
  /** Whether messages the conversation came with are still held back below what is
   * shown. */
  heldBelow?: boolean;
  /** Whether messages it came with are still held back above what is shown, drawn or
   * not. */
  heldAbove?: boolean;
  /** Told where the reader is, each time that is read: their place, or nothing while
   * the end is followed. */
  remember?: (spot: Spot | null) => void;
  /** Show the next step of the messages held back below, now: they are on the page
   * once React has drawn the update this makes, before the browser paints. */
  showBelow?: () => void;
  /** The same, for the next step of those held back above. */
  showAbove?: () => void;
} = {}): {
  /** For the element that scrolls. */
  scroller: RefObject<HTMLDivElement | null>;
  /** For the element inside it that grows. */
  content: RefObject<HTMLDivElement | null>;
  /** Whether the reader has scrolled away from the end, and there is more below, or
   * messages are held back below what is shown. */
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
  // Where the conversation opens, as the hook was first given it, and whether the
  // reader was put there: once, also when React runs the hook's start twice on purpose
  // (StrictMode).
  const opensAt = useRef(at);
  const opened = useRef(false);
  // What the page said last, for the listeners, which are set up once.
  const endHeld = useRef(heldBelow);
  const startHeld = useRef(heldAbove);
  const tell = useRef(remember);
  const more = useRef(showBelow);
  const older = useRef(showAbove);
  // The frame at which the reader's place is to be read, once a scroll of theirs asked
  // for it.
  const reading = useRef(0);
  // The place the conversation opens at, while the reader is still to be put there:
  // what was shown held them short of it, and more is being shown.
  const reaching = useRef<Spot | null>(null);
  // Whether more is being shown above to fill the window, at the end.
  const filling = useRef(false);

  useLayoutEffect(() => {
    endHeld.current = heldBelow;
    startHeld.current = heldAbove;
    tell.current = remember;
    more.current = showBelow;
    older.current = showAbove;
  });

  const toEnd = useCallback(() => {
    const scrolling = scroller.current;
    if (scrolling !== null) {
      scrolling.scrollTop = scrolling.scrollHeight;
      top.current = scrolling.scrollTop;
    }
  }, []);

  /** Whether what is shown is less than the window is high: there is nothing to
   * scroll. A window of no height, as on a page not laid out, is filled by anything. */
  const unfilled = useCallback(() => {
    const scrolling = scroller.current;
    return (
      scrolling !== null &&
      scrolling.clientHeight > 0 &&
      scrolling.scrollHeight <= scrolling.clientHeight
    );
  }, []);

  /** At the end, while it is followed: when what is shown doesn't fill the window and
   * messages are held back above, show the next step of them now. Once it is drawn,
   * before the browser paints, the reader is put at the end again and this asks again
   * (`filling`). */
  const fill = useCallback(() => {
    filling.current = false;
    if (
      following.current &&
      startHeld.current &&
      older.current !== undefined &&
      unfilled()
    ) {
      filling.current = true;
      older.current();
    }
  }, [unfilled]);

  /** Say where the reader is: nothing while the end is followed, and otherwise their
   * place, which takes the page's layout. Nothing at all, and what was said last
   * stands, when the element that scrolls is gone from the page or has no box: every
   * box in it would have a size of nothing then, which reads as no place. Nor while
   * the reader is still being put at the place it opens at, where they are isn't
   * theirs: React's StrictMode undoes and runs again the effect below, which says it,
   * in that moment. */
  const say = useCallback(() => {
    const scrolling = scroller.current;
    const growing = content.current;
    if (
      scrolling === null ||
      growing === null ||
      reaching.current !== null ||
      !scrolling.isConnected ||
      scrolling.getBoundingClientRect().height === 0
    ) {
      return;
    }
    tell.current?.(following.current ? null : spotIn(scrolling, growing));
  }, []);

  const follow = useCallback(() => {
    following.current = true;
    setAway(false);
    toEnd();
    // A reader at the end has no place.
    tell.current?.(null);
    fill();
  }, [toEnd, fill]);

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

  /** Put the reader at `spot` as the conversation opens, away from its end. Says
   * whether its message is shown. When what is shown holds them short of it, or doesn't
   * fill the window, the next step of the messages held back is shown, below or else
   * above, and they are put there again once it is drawn (`reaching`). */
  const reach = useCallback(
    (spot: Spot): boolean => {
      reaching.current = null;
      const scrolling = scroller.current;
      const growing = content.current;
      const short =
        scrolling === null || growing === null
          ? null
          : goTo(scrolling, growing, spot);
      if (scrolling === null || short === null) {
        return false;
      }
      following.current = false;
      top.current = scrolling.scrollTop;
      setAway(
        scrolling.scrollHeight - scrolling.scrollTop - scrolling.clientHeight >=
          NEAR,
      );
      // Short of the place, only what is below it can bring the reader there; what is
      // above it changes nothing of the room below. So steps above are asked for only
      // to fill a window that isn't filled, when nothing is held back below: asked for
      // while the reader was short, they were drawn until none was left, all 600
      // messages of a long conversation in one frame. A reader who is short of the place
      // with nothing below, in a window that is filled, as in a window taller than the
      // one they left it in, stays as near it as the page lets them.
      const filled = !unfilled();
      const next = endHeld.current
        ? short > 1 || !filled
          ? more.current
          : undefined
        : startHeld.current && !filled
          ? older.current
          : undefined;
      if (next !== undefined) {
        reaching.current = spot;
        next();
      }
      return true;
    },
    [unfilled],
  );

  // What was asked for to fill the window is drawn now: the reader is put at their
  // place again, or at the end, and more is asked for if it isn't enough yet. An update
  // made in a layout effect is drawn before the browser paints, and its layout effects
  // run before it paints too: so the first frame that shows the conversation is at the
  // place, or at the end with the window filled.
  useLayoutEffect(() => {
    const spot = reaching.current;
    if (spot !== null) {
      reach(spot);
    } else if (filling.current) {
      toEnd();
      fill();
    }
  });

  // At the end, or at the place where the reader left the conversation, before the
  // browser first paints it. The sizes below are watched from an effect, which React
  // runs once the browser has painted what it was given, and are first reported after
  // that: until then the conversation was painted from its top (ADR 0015, revision "a
  // long conversation opens newest first").
  //
  // And where the reader is, is said when the conversation leaves the page: before its
  // elements are taken off it, which they are once this is undone.
  useLayoutEffect(() => {
    const spot = opensAt.current;
    if (spot === null) {
      toEnd();
      fill();
    } else if (!opened.current) {
      opened.current = true;
      if (!reach(spot)) {
        // Its message isn't shown, which the page sees to: the end, as without one.
        toEnd();
        fill();
      }
    }
    return () => {
      cancelAnimationFrame(reading.current);
      reading.current = 0;
      say();
    };
  }, [toEnd, say, reach, fill]);

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
        // Where they are now is read at the next frame, once however often the
        // browser tells of their scroll before it. A move of the page's own changes
        // nothing of their place: it is the page's to keep.
        if (reading.current === 0) {
          reading.current = requestAnimationFrame(() => {
            reading.current = 0;
            say();
          });
        }
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
        // what is shown get shorter again, they are moved from where they are. The end
        // of what is shown is the conversation's only once nothing is held back below
        // it: followed before, it would take them on with each step that comes.
        movedFrom.current = null;
        if (below() < NEAR && !endHeld.current) {
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
  }, [toEnd, say]);

  return {
    scroller,
    content,
    // Held back below, the conversation's end is below what is shown, however close
    // the reader is to the end of that.
    away: away || heldBelow,
    follow,
    untilStill,
    standing,
    addedAbove,
  };
}
