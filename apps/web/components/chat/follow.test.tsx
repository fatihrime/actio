// When the page keeps the newest words in view, and when it leaves the reader alone.
//
// The simulated page lays nothing out, so a test says how tall things are, and when
// they changed size. What a real browser does with it is tried by hand, and by the
// end-to-end tests once they exist.
import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

import { useFollow } from "./follow";

/** As much of the conversation as fits on the screen, in pixels. */
const ROOM = 500;

function Page() {
  const { scroller, content, away, follow } = useFollow();
  return (
    <>
      <div ref={scroller} data-testid="scroller">
        <div ref={content} />
      </div>
      {away && (
        <button type="button" onClick={follow}>
          Newest
        </button>
      )}
    </>
  );
}

let resized: () => void = () => undefined;

beforeEach(() => {
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

/** A conversation of `height` pixels on the page, and what a browser would do to it. */
function conversation(height: number) {
  render(<Page />);
  const scroller = screen.getByTestId("scroller");
  let tall = height;
  let top = 0;
  Object.defineProperties(scroller, {
    clientHeight: { get: () => ROOM },
    scrollHeight: { get: () => tall },
    scrollTop: {
      get: () => top,
      // A browser never scrolls past the end.
      set: (to: number) => (top = Math.max(0, Math.min(to, tall - ROOM))),
    },
  });
  const end = () => tall - ROOM;
  return {
    /** How far down the reader is. */
    at: () => top,
    end,
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
    /** The reader scrolls. */
    scroll(to: number) {
      scroller.scrollTop = to;
      fireEvent.scroll(scroller);
    },
  };
}

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
