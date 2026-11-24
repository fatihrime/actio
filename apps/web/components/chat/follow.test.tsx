// When the page keeps the newest words in view, and when it leaves the reader alone.
//
// The simulated page lays nothing out, so a test says how tall things are, and when
// they changed size. What a real browser does with it is tried by hand, and by the
// end-to-end tests (e2e/scrolling.spec.ts, e2e/newest-first.spec.ts). Those run
// Chromium alone: how Firefox and WebKit scroll while older messages are shown above
// the reader is tried by hand (ADR 0015, revision "a long conversation opens newest
// first").
import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Activity, useEffect, useLayoutEffect, useRef } from "react";
import { beforeEach, expect, test, vi } from "vitest";

import type { Spot } from "@/lib/chat/places";
import { clockAt } from "@/tests/clock";
import { holdFrames } from "@/tests/frames";

import { STILL_AFTER_MS, useFollow } from "./follow";

/** As much of the conversation as fits on the screen, in pixels. */
const ROOM = 500;

/** How tall a message is, where a test has messages on the page and doesn't say what
 * they are drawn in, in pixels. */
const TALL = 100;

type Follow = ReturnType<typeof useFollow>;

/** What the page hands the hook. */
type Given = Parameters<typeof useFollow>[0];

/** A block a message is drawn in: how tall it is, in pixels, and 0 for one that isn't
 * drawn, such as what a folded thought holds; or the blocks inside it, one under
 * another. */
type Block = number | Block[];

/** A message on the page: its id, and the blocks it is drawn in, one under another
 * inside one element, as markdown is, which stands `INSET` pixels inside the message's
 * own top and bottom. Its id alone for a message `TALL` pixels high, with nothing
 * inside. */
type Shown = string | { id: string; blocks: Block[] };

/** How far inside a message drawn in blocks the element that holds them stands, above
 * and below, in pixels: so the two can be told apart. */
const INSET = 10;

/** How tall `block` is. */
function heightOf(block: Block): number {
  return typeof block === "number"
    ? block
    : block.reduce<number>((sum, inside) => sum + heightOf(inside), 0);
}

/** `blocks` as elements: a paragraph for each height, an element for each group. */
function Blocks({ blocks }: { blocks: Block[] }) {
  return blocks.map((block, index) =>
    typeof block === "number" ? (
      <p key={index} />
    ) : (
      <div key={index}>
        <Blocks blocks={block} />
      </div>
    ),
  );
}

function Page({
  handOver,
  painting,
  given,
  messages = [],
}: {
  /** Given what the hook hands the page, after each render. */
  handOver?: (follows: Follow) => void;
  /** Called after the hook's own layout effects: where the page is when the browser
   * first paints it. */
  painting?: () => void;
  /** What the page hands the hook, if anything: without it, the conversation opens at
   * its end, and nobody is told where the reader is. */
  given?: Given;
  /** The first messages shown, one under another. */
  messages?: Shown[];
}) {
  const follows = useFollow(given);
  const { scroller, content, away, follow } = follows;
  useLayoutEffect(() => {
    painting?.();
  }, [painting]);
  useEffect(() => {
    handOver?.(follows);
  });
  return (
    <>
      <div ref={scroller} data-testid="scroller">
        <LaidOut />
        <div ref={content}>
          {/* Older messages drawn and not shown yet, then the first that are. */}
          <div hidden />
          <div data-testid="shown">
            {messages.map((message) =>
              typeof message === "string" ? (
                <article key={message} data-message-id={message} />
              ) : (
                <article key={message.id} data-message-id={message.id}>
                  <div>
                    <Blocks blocks={message.blocks} />
                  </div>
                </article>
              ),
            )}
          </div>
        </div>
      </div>
      {away && (
        <button type="button" onClick={follow}>
          Newest
        </button>
      )}
    </>
  );
}

/** Gives the element that scrolls the sizes a test made up (`laidOut`), as soon as it
 * is on the page: the hook's effects then find it as a browser would, laid out. React
 * runs this layout effect before those of the page around it. */
function LaidOut() {
  const here = useRef<HTMLSpanElement>(null);
  useLayoutEffect(() => {
    const scroller = here.current?.closest<HTMLElement>(
      '[data-testid="scroller"]',
    );
    if (scroller !== null && scroller !== undefined) {
      laidOut(scroller);
    }
  }, []);
  return <span ref={here} />;
}

let laidOut: (scroller: HTMLElement) => void = () => undefined;
let resized: () => void = () => undefined;

beforeEach(() => {
  laidOut = () => undefined;
  vi.stubGlobal(
    "ResizeObserver",
    class {
      constructor(changed: () => void) {
        resized = changed;
      }
      observe() {
        // The test says when something changed size.
      }
      disconnect() {
        // Nothing to let go of.
      }
    },
  );
});

/** A conversation of `height` pixels on the page, and what a browser would do to it.
 * Nothing has said yet that a size changed: the test does, with `resize`. The page hands
 * the hook what is `given`, and shows `messages` first, from the top of what is shown.
 * While the page is hidden (`hide`), or gone from the document, nothing on it has a
 * box, as in a browser; nor has a block of no height. */
function conversation(
  height: number,
  {
    painting,
    given,
    messages = [],
  }: { painting?: () => void; given?: Given; messages?: Shown[] } = {},
) {
  let tall = height;
  let top = 0;
  // How far down the first message that is shown is.
  let above = 0;
  laidOut = (scroller) => {
    const drawn = () =>
      scroller.isConnected && scroller.style.display !== "none";
    // A box `down` pixels below the top of what is shown, as the screen has it.
    const box = (down: number, high: number) =>
      drawn() && high > 0
        ? new DOMRect(0, above + down - top, 800, high)
        : new DOMRect();
    const boxed = (element: Element, down: number, high: number) => {
      Object.defineProperty(element, "getBoundingClientRect", {
        value: () => box(down, high),
      });
    };
    // Each of `blocks` gives its element its box, from `down` on.
    const lay = (elements: HTMLCollection, blocks: Block[], down: number) => {
      let from = down;
      blocks.forEach((block, at) => {
        const element = elements[at];
        if (element !== undefined) {
          boxed(element, from, heightOf(block));
          if (typeof block !== "number") {
            lay(element.children, block, from);
          }
        }
        from += heightOf(block);
      });
    };
    Object.defineProperties(scroller, {
      clientHeight: { get: () => ROOM },
      scrollHeight: { get: () => tall },
      scrollTop: {
        get: () => top,
        // A browser never scrolls past the end.
        set: (to: number) => (top = Math.max(0, Math.min(to, tall - ROOM))),
      },
      // The top of the screen's room for the conversation.
      getBoundingClientRect: {
        value: () => (drawn() ? new DOMRect(0, 0, 800, ROOM) : new DOMRect()),
      },
    });
    Object.defineProperty(
      scroller.querySelector('[data-testid="shown"]'),
      "offsetTop",
      { get: () => above },
    );
    let down = 0;
    scroller.querySelectorAll("article").forEach((article, index) => {
      const message = messages[index];
      const blocks =
        message === undefined || typeof message === "string"
          ? null
          : message.blocks;
      const holder = article.firstElementChild;
      if (blocks === null || holder === null) {
        boxed(article, down, TALL);
        down += TALL;
        return;
      }
      const inside = heightOf(blocks);
      boxed(article, down, INSET + inside + INSET);
      boxed(holder, down + INSET, inside);
      lay(holder.children, blocks, down + INSET);
      down += INSET + inside + INSET;
    });
  };
  let follows: Follow | undefined;
  let mode: "visible" | "hidden" = "visible";
  let handed = given;
  const page = () => (
    <Activity mode={mode}>
      <Page
        handOver={(followed) => {
          follows = followed;
        }}
        painting={painting}
        given={handed}
        messages={messages}
      />
    </Activity>
  );
  const { rerender, unmount } = render(page());
  const scroller = screen.getByTestId("scroller");
  const end = () => tall - ROOM;
  const hook = (): Follow => {
    if (follows === undefined) {
      throw new Error("The page handed over nothing of the hook.");
    }
    return follows;
  };
  return {
    /** How far down the reader is. */
    at: () => top,
    end,
    /** The element that scrolls. */
    scroller,
    /** How long until the reader is still, as the hook says. */
    untilStill: () => hook().untilStill(),
    /** The page hands the hook something else. */
    give(changed: Given) {
      handed = changed;
      rerender(page());
    },
    /** The conversation stays on the page, hidden, as React keeps what is behind a
     * Suspense boundary's fallback or in an Activity that is hidden. */
    hide() {
      mode = "hidden";
      rerender(page());
    },
    /** The conversation leaves the page. */
    leave() {
      unmount();
    },
    /** The conversation becomes `to` pixels tall. A browser that was scrolled past
     * the new end moves up to it, and says it scrolled. */
    resize(to: number) {
      const before = top;
      tall = to;
      top = Math.min(top, end());
      act(() => {
        if (top !== before) {
          fireEvent.scroll(scroller);
        }
        resized();
      });
    },
    /** The reader scrolls. The browser says so at once, or with `told: false` only
     * at the next scroll it tells of. */
    scroll(to: number, { told = true } = {}) {
      scroller.scrollTop = to;
      if (told) {
        fireEvent.scroll(scroller);
      }
    },
    /** Older messages `added` pixels tall are shown above what is shown, as the page
     * shows them: the hook is asked where the reader stands just before, and told just
     * after. In the same change, what is at the end may have `grown`, and the browser
     * may have `kept` the reader's place itself. A browser whose reader was moved says
     * that they scrolled. It tells of the new size later, when the test calls
     * `resize`. */
    addAbove(added: number, { grown = 0, kept = false } = {}) {
      const { standing, addedAbove } = hook();
      const before = top;
      act(() => {
        const stood = standing();
        above += added;
        tall += added + grown;
        if (kept) {
          scroller.scrollTop = top + added;
        }
        if (stood !== null) {
          addedAbove(stood);
        }
        if (top !== before) {
          fireEvent.scroll(scroller);
        }
      });
    },
  };
}

test("the page is laid out before its sizes are watched", () => {
  // Safari's engine reports an error of the page at every page load otherwise: it
  // tells a new observer the sizes from before the layout, and then finds them changed
  // (ADR 0015). The simulated page lays nothing out, so this holds the order alone;
  // what the browser makes of it is measured in that engine.
  const order: string[] = [];
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe(watched: Element) {
        order.push(`watch ${watched.getAttribute("data-testid") ?? "content"}`);
      }
      disconnect() {
        // Nothing to let go of.
      }
    },
  );
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
    function (this: HTMLElement) {
      order.push(`lay out ${this.getAttribute("data-testid") ?? "content"}`);
      return new DOMRect();
    },
  );

  render(<Page />);

  expect(order).toEqual([
    "lay out scroller",
    "watch content",
    "watch scroller",
  ]);
});

test("the page is at the end when it is first painted, before any size was told", () => {
  // Sizes are watched from an effect, which React runs once the browser has painted.
  // A page that went to the end only when they were told was painted at its top first.
  let painted: number | null = null;

  const page = conversation(2000, {
    painting: () => {
      painted = screen.getByTestId("scroller").scrollTop;
    },
  });

  expect(painted).toBe(1500);
  expect(page.at()).toBe(1500);
});

test("the end stays in view while an answer grows", () => {
  const page = conversation(400);

  page.resize(900);
  expect(page.at()).toBe(400);

  page.resize(2000);
  expect(page.at()).toBe(1500);
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});

test("a reader who scrolled up is left where they are", () => {
  const page = conversation(2000);
  page.resize(2000);

  page.scroll(700);
  page.resize(3000);

  expect(page.at()).toBe(700);
  expect(screen.getByRole("button", { name: "Newest" })).toBeVisible();
});

test("scrolling up a little is scrolling up", () => {
  // A hand on a trackpad starts with a few pixels. Pulled back to the end at each
  // new word, the reader could never get away from it.
  const page = conversation(2000);
  page.resize(2000);

  page.scroll(page.end() - 4);
  page.resize(2100);

  expect(page.at()).toBe(1496);
});

test("the end is followed again once the reader is back at it", () => {
  const page = conversation(2000);
  page.resize(2000);
  page.scroll(700);

  page.scroll(page.end() - 20); // close enough
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
  page.resize(3000);

  expect(page.at()).toBe(2500);
});

test("an answer that gets shorter isn't the reader scrolling up", () => {
  // The reasoning folds away when the answer begins.
  const page = conversation(2000);
  page.resize(2000);

  page.resize(1200);
  expect(page.at()).toBe(700);
  page.resize(1800);

  expect(page.at()).toBe(1300);
});

test("the button goes to the end, and stays there", async () => {
  const user = userEvent.setup();
  const page = conversation(2000);
  page.resize(2000);
  page.scroll(700);

  await user.click(screen.getByRole("button", { name: "Newest" }));

  expect(page.at()).toBe(1500);
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
  page.resize(2600);
  expect(page.at()).toBe(2100);
});

test("the button waits until there is something below", () => {
  const page = conversation(2000);
  page.resize(2000);

  page.scroll(page.end() - 10);
  expect(screen.queryByRole("button")).not.toBeInTheDocument();

  page.resize(2400); // the answer went on, and the reader stayed
  expect(screen.getByRole("button", { name: "Newest" })).toBeVisible();
});

// A wheel turned is heard before the browser says that the reader scrolled, which it
// does a frame later: a change of size meanwhile would take them back to the end.

test("a wheel turned up leaves the end at once", () => {
  const page = conversation(2000);

  fireEvent.wheel(page.scroller, { deltaY: -100 });
  page.resize(3000); // before the browser says that the reader scrolled

  expect(page.at()).toBe(1500);
  expect(screen.getByRole("button", { name: "Newest" })).toBeVisible();
});

test.each([
  { how: "turned up with nothing above", height: 400, deltaY: -100, to: 900 },
  { how: "turned down", height: 2000, deltaY: 100, to: 3000 },
  // Ctrl and the wheel, or two fingers moved apart on a trackpad, which a browser
  // reports the same way: the page gets larger, and nothing scrolls.
  {
    how: "turned up to make the page larger",
    height: 2000,
    deltaY: -100,
    ctrlKey: true,
    to: 3000,
  },
])(
  "a wheel $how leaves the end followed",
  ({ height, deltaY, ctrlKey = false, to }) => {
    const page = conversation(height);

    fireEvent.wheel(page.scroller, { deltaY, ctrlKey });
    page.resize(to);

    expect(page.at()).toBe(page.end());
  },
);

// What is shown can get shorter for a moment under a reader who is a few pixels above
// its end: the browser moves them up to the new end. Seen in Chromium on a busy
// device, a few frames after a wheel was turned: when it got longer again, the browser
// put the reader back where the wheel had left them, by itself.

test("a reader the browser puts back after what is shown got shorter isn't scrolling down", () => {
  // As recorded, with the sizes of this page: the wheel, the browser saying that the
  // reader scrolled, shorter by 20, longer by 24 with the reader put back, and on.
  const page = conversation(2000);
  page.resize(2000);
  fireEvent.wheel(page.scroller, { deltaY: -6 });
  page.scroll(1494);

  page.resize(1980); // the browser moves the reader up to the new end, 1480
  page.resize(2004);
  page.scroll(1494); // and puts them back
  page.resize(2100); // the answer goes on

  expect(page.at()).toBe(1494);
  expect(screen.getByRole("button", { name: "Newest" })).toBeVisible();
});

test("a reader put back only part of the way isn't scrolling down either", () => {
  const page = conversation(2000);
  page.resize(2000);
  fireEvent.wheel(page.scroller, { deltaY: -6 });
  page.scroll(1494);

  page.resize(1980);
  page.resize(1990); // room for some of the way back: its end is 1490
  page.scroll(1490);
  page.resize(2100);

  expect(page.at()).toBe(1490);
  expect(screen.getByRole("button", { name: "Newest" })).toBeVisible();
});

test("a reader put back in two steps isn't scrolling down at the second either", () => {
  const page = conversation(2000);
  page.resize(2000);
  fireEvent.wheel(page.scroller, { deltaY: -6 });
  page.scroll(1494);

  page.resize(1980);
  page.resize(1990);
  page.scroll(1490); // as far as there is room
  page.resize(2004);
  page.scroll(1494); // and the rest of the way
  page.resize(2100);

  expect(page.at()).toBe(1494);
  expect(screen.getByRole("button", { name: "Newest" })).toBeVisible();
});

test("a reader who scrolled up after they were moved is followed again once back near the end", () => {
  // Nothing of theirs is heard here but the scrolls themselves, as when a finger's
  // fling goes on: the place they were moved from is behind them once they scroll up.
  const page = conversation(2000);
  page.resize(2000);
  fireEvent.wheel(page.scroller, { deltaY: -6 });
  page.scroll(1494);
  page.resize(1980); // moved up to 1480, from 1494

  page.scroll(1200);
  page.resize(2004);
  page.scroll(1490);
  page.resize(2100);

  expect(page.at()).toBe(1600);
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});

test("a reader who scrolled down past where they were moved from is moved from where they are next", () => {
  const page = conversation(2000);
  page.resize(2000);
  fireEvent.wheel(page.scroller, { deltaY: -6 });
  page.scroll(1494);
  page.resize(1980); // moved up to 1480, from 1494
  page.resize(2600);
  page.scroll(2000); // their own scroll down, a hundred from the end

  page.resize(2480); // shorter again: moved up to 1980, from 2000
  page.resize(2520);
  page.scroll(2000); // and put back
  page.resize(2700);

  expect(page.at()).toBe(2000);
  expect(screen.getByRole("button", { name: "Newest" })).toBeVisible();
});

test("a reader put back a fraction of a pixel off isn't scrolling down either", () => {
  // A scroller's place isn't a whole number at every zoom.
  const page = conversation(2000);
  page.resize(2000);
  fireEvent.wheel(page.scroller, { deltaY: -6 });
  page.scroll(1494);

  page.resize(1980);
  page.resize(2004);
  page.scroll(1494.4);
  page.resize(2100);

  expect(page.at()).toBe(1494.4);
  expect(screen.getByRole("button", { name: "Newest" })).toBeVisible();
});

test("where the reader was is the first place, when what is shown gets shorter twice", () => {
  const page = conversation(2000);
  page.resize(2000);
  fireEvent.wheel(page.scroller, { deltaY: -6 });
  page.scroll(1494);

  page.resize(1980); // moved up to 1480
  page.resize(1960); // and to 1460
  page.resize(2004);
  page.scroll(1494); // put back where the wheel had left them
  page.resize(2100);

  expect(page.at()).toBe(1494);
  expect(screen.getByRole("button", { name: "Newest" })).toBeVisible();
});

test("older messages shown above move the place the reader was moved from along", () => {
  const page = conversation(2000);
  page.resize(2000);
  fireEvent.wheel(page.scroller, { deltaY: -6 });
  page.scroll(1494);
  page.resize(1980); // moved up to 1480

  page.addAbove(1000); // the reader is at 2480, and was moved from 2494
  page.resize(3004);
  page.scroll(2494); // put back
  page.resize(3100);

  expect(page.at()).toBe(2494);
  expect(screen.getByRole("button", { name: "Newest" })).toBeVisible();
});

test.each([
  {
    how: "turns the wheel",
    act: (scroller: HTMLElement) => fireEvent.wheel(scroller, { deltaY: 6 }),
  },
  {
    how: "moves a finger",
    act: (scroller: HTMLElement) => fireEvent.touchMove(scroller),
  },
  {
    how: "takes hold of the scrollbar",
    act: (scroller: HTMLElement) => fireEvent.pointerDown(scroller),
  },
  {
    how: "presses a key that scrolls",
    act: () => fireEvent.keyDown(window, { key: "ArrowDown" }),
  },
])(
  "a reader who $how after what is shown got shorter is followed again near the end",
  ({ act: theirs }) => {
    // The control: what they do next is theirs, back to the end as well.
    const page = conversation(2000);
    page.resize(2000);
    fireEvent.wheel(page.scroller, { deltaY: -6 });
    page.scroll(1494);
    page.resize(1980);
    page.resize(2004);

    theirs(page.scroller);
    page.scroll(1490);
    page.resize(2100);

    expect(page.at()).toBe(1600);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  },
);

test("a reader who is past where they were moved from is followed again near the end", () => {
  // Without anything the page hears of them, as a finger's fling goes on: beyond the
  // place the browser moved them from, a scroll down is theirs.
  const page = conversation(2000);
  page.resize(2000);
  fireEvent.wheel(page.scroller, { deltaY: -6 });
  page.scroll(1494);
  page.resize(1980);
  page.resize(2040);

  page.scroll(1520);
  page.resize(2100);

  expect(page.at()).toBe(1600);
});

// Older messages are shown above what is shown while a long conversation comes onto
// the page, newest first (chat.tsx). Browsers keep a reader's place by themselves, but
// none does at the top of what is shown, where a reader who wants the older messages
// goes.

test("older messages shown above leave a reader at the end there", () => {
  const page = conversation(2000);

  page.addAbove(1000);

  expect(page.at()).toBe(2500);
  expect(page.at()).toBe(page.end());
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});

test("older messages shown above leave a reader who scrolled up at what they read", () => {
  const page = conversation(2000);
  page.scroll(700);

  page.addAbove(1000);

  // What they read moved down by what was shown, and they with it.
  expect(page.at()).toBe(1700);
  expect(screen.getByRole("button", { name: "Newest" })).toBeVisible();
});

test("what grew at the end as older messages were shown doesn't move the reader", () => {
  // An answer may grow at the end in the same change: the conversation is taller by
  // more than what was shown above.
  const page = conversation(2000);
  page.scroll(700);

  page.addAbove(1000, { grown: 300 });

  expect(page.at()).toBe(1700);
});

test("a reader whose place the browser kept is moved no further", () => {
  const page = conversation(2000);
  page.scroll(700);

  page.addAbove(1000, { kept: true });

  expect(page.at()).toBe(1700);
});

test("a scroll up told of only after messages were shown above is a scroll up", () => {
  // The reader scrolls up, and before the browser says so, messages are shown above.
  // Measured from where they were before the messages came, the reader would have gone
  // down: the end would be followed still, and pull them back at its next change.
  const page = conversation(2000);
  page.scroll(800, { told: false }); // 700 up from the end

  page.addAbove(1000); // and the browser says that they scrolled

  expect(page.at()).toBe(1800);
  expect(screen.getByRole("button", { name: "Newest" })).toBeVisible();
  page.resize(3400); // the answer at the end goes on
  expect(page.at()).toBe(1800);
});

// Older messages are shown only while the reader is still: in Firefox and WebKit a
// change above the reader overrides a scroll that is under way.

test("the reader is still a moment after they last scrolled, by the clock", () => {
  // As the decision's record says (ADR 0015).
  expect(STILL_AFTER_MS).toBe(150);
  const clock = clockAt();
  const page = conversation(2000);
  expect(page.untilStill()).toBe(0);

  page.scroll(700);

  expect(page.untilStill()).toBe(150);
  clock.advance(149);
  expect(page.untilStill()).toBe(1);
  clock.advance(1);
  expect(page.untilStill()).toBe(0);
});

test.each<{ what: string; does: (scroller: HTMLElement) => void }>([
  {
    what: "a wheel turned",
    does: (scroller) => fireEvent.wheel(scroller, { deltaY: 100 }),
  },
  {
    what: "a finger that moves",
    does: (scroller) => fireEvent.touchMove(scroller),
  },
  {
    what: "a scrollbar taken hold of",
    does: (scroller) => fireEvent.pointerDown(scroller),
  },
  ...["PageUp", "PageDown", "Home", "End", "ArrowUp", "ArrowDown", " "].map(
    (key) => ({
      what: `the key "${key}", wherever it is pressed`,
      does: () => fireEvent.keyDown(document.body, { key }),
    }),
  ),
])("$what is the reader scrolling", ({ does }) => {
  const clock = clockAt();
  const page = conversation(2000);

  does(page.scroller);

  expect(page.untilStill()).toBe(150);
  clock.advance(150);
  expect(page.untilStill()).toBe(0);
});

test.each<{
  what: string;
  does: (page: ReturnType<typeof conversation>) => void;
}>([
  {
    what: "the page following the end",
    does: (page) => {
      page.resize(3000);
      fireEvent.scroll(page.scroller); // the browser tells of the page's move
    },
  },
  {
    what: "the page showing older messages above",
    does: (page) => {
      page.addAbove(1000);
    },
  },
  {
    what: "a key that doesn't scroll",
    does: () => fireEvent.keyDown(document.body, { key: "a" }),
  },
  {
    what: "a scroll of less than a pixel",
    does: (page) => {
      page.scroll(1499.5);
    },
  },
])("$what is not the reader scrolling", ({ does }) => {
  clockAt();
  const page = conversation(2000);

  does(page);

  expect(page.untilStill()).toBe(0);
});

test("a clock that was put back leaves the reader still", () => {
  const clock = clockAt();
  const page = conversation(2000);
  page.scroll(700);

  clock.advance(-1000);

  expect(page.untilStill()).toBe(0);
});

// A conversation its reader left away from its end opens where they left it: at the
// block of a message at the top of the screen, with the top of the screen as far below
// the block's top as it was. Where the reader is, the hook says: read after they
// scrolled, once a frame, and when the conversation leaves the page.

/** The ids of `count` messages: "m0", "m1", and so on. */
function ids(count: number): string[] {
  return Array.from({ length: count }, (_, index) => `m${String(index)}`);
}

test("a conversation opens at the place where its reader left it, from its first paint, and its end isn't followed", () => {
  let painted: number | null = null;

  const page = conversation(2000, {
    messages: ids(20),
    given: { at: { message: "m3", path: [], offset: 30 } },
    painting: () => {
      painted = screen.getByTestId("scroller").scrollTop;
    },
  });

  // "m3" is 300 down: the screen begins 30 below its top.
  expect(painted).toBe(330);
  expect(screen.getByRole("button", { name: "Newest" })).toBeVisible();
  page.resize(3000);
  expect(page.at()).toBe(330);
});

test("a place is the block of its message at the top of the screen: a block above it that is another height when the conversation is drawn anew moves nothing", () => {
  holdFrames();
  // "m3" is drawn in four blocks, the second of them 352 pixels high when the reader
  // left, and 218 when they came back. So was a block of code, until a rule of the
  // stylesheet gave code its real height from the first layout (app/globals.css);
  // so may be a diagram, which is drawn later.
  const drawn = (second: number): Shown[] => [
    ...ids(3),
    { id: "m3", blocks: [50, second, 60, 40] },
    ...ids(20).slice(4),
  ];
  const told: (Spot | null)[] = [];
  const before = conversation(3000, {
    messages: drawn(352),
    given: { remember: (spot) => told.push(spot) },
  });
  // The reader read the second block, and went on: the screen begins 20 into the
  // third, which is 300 + 10 + 50 + 352 down.
  before.scroll(732);
  before.leave();
  expect(told).toEqual([{ message: "m3", path: [0, 2], offset: 20 }]);

  const after = conversation(3000, {
    messages: drawn(218),
    given: { at: told[0] ?? null },
  });

  // The third block is 300 + 10 + 50 + 218 down now.
  expect(after.at()).toBe(598);
});

test("a place is the block at the top of the screen, found among blocks of which some aren't drawn", () => {
  holdFrames();
  const told: (Spot | null)[] = [];
  // The third of "m3"'s blocks isn't drawn: its box is nothing at all, which a search
  // by halves that took it for a box would put above the screen, and go past the second.
  const page = conversation(3000, {
    messages: [
      ...ids(3),
      { id: "m3", blocks: [50, 60, 0, 40] },
      ...ids(20).slice(4),
    ],
    given: { remember: (spot) => told.push(spot) },
  });
  // The screen begins 20 into the second block, which is 300 + 10 + 50 down.
  page.scroll(380);

  page.leave();

  expect(told).toEqual([{ message: "m3", path: [0, 1], offset: 20 }]);
});

test("a place whose block isn't drawn now goes down its way as far as there is, and no further than the end of what it finds", () => {
  // Its message is drawn in two blocks now, and the place was in the fourth of four:
  // the element that holds the two, from 310 down and 110 high, is as far as it goes.
  // From the message itself, from 300 down and 130 high, the reader would be at 430.
  const page = conversation(3000, {
    messages: [...ids(3), { id: "m3", blocks: [50, 60] }, ...ids(20).slice(4)],
    given: { at: { message: "m3", path: [0, 3], offset: 500 } },
  });

  expect(page.at()).toBe(420);
});

test("a place in a thought that is folded when the conversation is drawn anew is the folded thought, and draws nothing below for it", () => {
  holdFrames();
  // "m3" begins with a thought: a summary 40 pixels high, and what it says under it,
  // which isn't drawn while the thought is folded, though it is still on the page.
  const drawn = (open: boolean): Shown[] => [
    ...ids(3),
    { id: "m3", blocks: [[40, open ? 260 : 0], 60, 40] },
    ...ids(20).slice(4),
  ];
  const told: (Spot | null)[] = [];
  const before = conversation(3000, {
    messages: drawn(true),
    given: { remember: (spot) => told.push(spot) },
  });
  // The reader reads in the thought, open: the screen begins 100 into what it says,
  // which is 300 + 10 + 40 down.
  before.scroll(450);
  before.leave();
  expect(told).toEqual([{ message: "m3", path: [0, 0, 1], offset: 100 }]);
  let asked = 0;

  const after = conversation(3000, {
    messages: drawn(false),
    given: {
      at: told[0] ?? null,
      heldBelow: true,
      showBelow: () => {
        asked += 1;
      },
    },
  });

  // At the folded thought, from 310 down and 40 high: no further than its end.
  expect(after.at()).toBe(350);
  expect(asked).toBe(0);
});

test("a place whose message isn't shown is no place: the conversation opens at its end", () => {
  const page = conversation(2000, {
    messages: ids(20),
    given: { at: { message: "elsewhere", path: [], offset: 30 } },
  });

  expect(page.at()).toBe(1500);
  page.resize(3000);
  expect(page.at()).toBe(2500);
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});

test("while messages are held back below what is shown, a reader at its end isn't at the conversation's end", () => {
  const at = { message: "m3", path: [], offset: 30 };
  const page = conversation(2000, {
    messages: ids(20),
    given: { at, heldBelow: true },
  });

  page.scroll(page.end()); // the reader goes to the end of what is shown
  expect(screen.getByRole("button", { name: "Newest" })).toBeVisible();
  page.resize(2600); // the next step comes in below

  expect(page.at()).toBe(1500);
  expect(screen.getByRole("button", { name: "Newest" })).toBeVisible();

  // Nothing is held back any more: the end of what is shown is the end.
  page.give({ at, heldBelow: false });
  page.scroll(page.end());
  page.resize(3000);
  expect(page.at()).toBe(2500);
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});

test("where the reader is, is read once a frame while they scroll, and not for the page's own moves", () => {
  const frame = holdFrames();
  const told: (Spot | null)[] = [];
  const page = conversation(2000, {
    messages: ids(20),
    given: { remember: (spot) => told.push(spot) },
  });

  page.scroll(700);
  page.scroll(650);
  page.scroll(640);
  expect(told).toEqual([]);
  act(frame);

  // "m6" is 600 down: the screen begins 40 below its top.
  expect(told).toEqual([{ message: "m6", path: [], offset: 40 }]);

  // Older messages shown above, and an answer that grows at the end.
  page.addAbove(1000);
  page.resize(3400);
  act(frame);
  expect(told).toHaveLength(1);
  expect(page.at()).toBe(1640);
});

test("a reader at the end has no place", async () => {
  const user = userEvent.setup();
  const frame = holdFrames();
  const told: (Spot | null)[] = [];
  const page = conversation(2000, {
    messages: ids(20),
    given: { remember: (spot) => told.push(spot) },
  });
  page.scroll(700);
  act(frame);

  page.scroll(page.end() - 20); // close enough
  act(frame);
  page.scroll(700);
  act(frame);
  await user.click(screen.getByRole("button", { name: "Newest" }));

  expect(told).toEqual([
    { message: "m7", path: [], offset: 0 },
    null,
    { message: "m7", path: [], offset: 0 },
    null,
  ]);
});

test.each([
  {
    where: "away from its end",
    goes: (page: ReturnType<typeof conversation>) => {
      page.scroll(640);
    },
    told: [{ message: "m6", path: [], offset: 40 }],
  },
  { where: "at its end", goes: () => undefined, told: [null] },
])(
  "where the reader is, is said when the conversation leaves the page $where",
  ({ goes, told }) => {
    // No frame comes in between.
    holdFrames();
    const said: (Spot | null)[] = [];
    const page = conversation(2000, {
      messages: ids(20),
      given: { remember: (spot) => said.push(spot) },
    });
    goes(page);

    page.leave();

    expect(said).toEqual(told);
  },
);

test("a conversation that is hidden, and kept, keeps its reader's place", () => {
  // React undoes the layout effects of what it hides before it hides it, so where the
  // reader is can still be read then.
  holdFrames();
  const said: (Spot | null)[] = [];
  const page = conversation(2000, {
    messages: ids(20),
    given: { remember: (spot) => said.push(spot) },
  });
  page.scroll(640);

  page.hide();

  expect(said).toEqual([{ message: "m6", path: [], offset: 40 }]);
  expect(page.scroller).not.toBeVisible();
  page.leave();
  expect(said).toHaveLength(1);
});

test("nothing is said of where the reader is when the conversation leaves the page while it isn't drawn", () => {
  // Every box in it has a size of nothing then, which would read as no place at all.
  holdFrames();
  const said: (Spot | null)[] = [];
  const page = conversation(2000, {
    messages: ids(20),
    given: { remember: (spot) => said.push(spot) },
  });
  page.scroll(640);
  page.scroller.style.display = "none";

  page.leave();

  expect(said).toEqual([]);
});

test("nothing is said of where the reader is when the element that scrolls is gone from the page by the frame that reads it", () => {
  const frame = holdFrames();
  const said: (Spot | null)[] = [];
  const page = conversation(2000, {
    messages: ids(20),
    given: { remember: (spot) => said.push(spot) },
  });
  page.scroll(640);
  const { parentElement: parent, nextSibling: next } = page.scroller;

  page.scroller.remove();
  act(frame);

  expect(said).toEqual([]);
  // Put back, for React to take it off the page itself.
  parent?.insertBefore(page.scroller, next);
});
