// An arriving answer is drawn at most every 50 ms (ADR 0016, revision "an arriving
// answer is drawn at most every 50 ms"): while it arrives, the text handed to the code
// that draws markdown changes no more often than that, counted from when the text
// before it was on the page, and it is whole in the very render in which the answer
// ends.
//
// The hook is tried first with a small page of the test's own, which notes each text
// it was handed once React has put it on the page, with whether it was arriving. Then
// through the app, where the code that draws markdown is a stand-in that notes each
// text the same way. The page holds that code for as long as it is open, so each of
// those tests opens the app from modules loaded anew, as newest-first.test.tsx does.
//
// The time is the test's clock (tests/clock.ts). Timers run as ever: while the clock
// stands still nothing newer is handed on, however long a test waits, and the newest is
// once the test has moved it on.
import { act, render, screen, waitFor } from "@testing-library/react";
import { StrictMode, useLayoutEffect } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { clockAt } from "@/tests/clock";
import {
  answer,
  conversation,
  fakeApi,
  json,
  question,
  sentByHand,
  turn,
} from "@/tests/fake-api";
import type { Call } from "@/tests/fake-api";
import { RECORDED } from "@/tests/recorded";

import { DRAWN_EVERY_MS, usePaced } from "./paced";

/** Give the page more than `DRAWN_EVERY_MS` of real time: long enough for a wait of
 * the hook's to be over, had it gone by real time, and for its timer to look again. */
async function aWhile(): Promise<void> {
  await act(
    () =>
      new Promise<void>((resolve) => {
        setTimeout(resolve, 3 * DRAWN_EVERY_MS);
      }),
  );
}

const STRICTNESS = [
  { how: "", strict: false },
  // React runs each effect, undoes it and runs it again when the page appears.
  { how: " under React's StrictMode", strict: true },
];

describe("the pace of an arriving text", () => {
  /** Each text the hook handed on, once React had put it on the page, with whether it
   * was arriving then. */
  let handedOn: { text: string; arriving: boolean }[] = [];
  /** Told of each text as it is put on the page: what it does takes as long as a draw
   * would. */
  let whileDrawn: (text: string) => void = () => undefined;

  beforeEach(() => {
    handedOn = [];
    whileDrawn = () => undefined;
  });

  function Paced({ text, arriving }: { text: string; arriving: boolean }) {
    return <Drawn text={usePaced(text, arriving)} arriving={arriving} />;
  }

  /** Shows what the hook handed on, and notes it once it is on the page. */
  function Drawn({ text, arriving }: { text: string; arriving: boolean }) {
    useLayoutEffect(() => {
      whileDrawn(text);
      handedOn.push({ text, arriving });
    }, [text, arriving]);
    return <p data-testid="drawn">{text}</p>;
  }

  /** The page with `text`, which may be `arriving`. `change` gives it another. */
  function paced(text: string, arriving: boolean, { strict = false } = {}) {
    const { rerender, unmount } = render(
      <Paced text={text} arriving={arriving} />,
      { wrapper: strict ? StrictMode : undefined },
    );
    return {
      change: (to: string, still: boolean) => {
        rerender(<Paced text={to} arriving={still} />);
      },
      unmount,
    };
  }

  /** What the page shows. */
  function shown(): string {
    return screen.getByTestId("drawn").textContent;
  }

  /** The texts the hook handed on, in order, each once. */
  function texts(): string[] {
    return [...new Set(handedOn.map(({ text }) => text))];
  }

  it("hands on a text that isn't arriving as it is, at once, also after it changed", () => {
    clockAt();
    const view = paced("Echo: ", false);
    expect(shown()).toBe("Echo: ");

    view.change("Echo: Why", false);
    expect(shown()).toBe("Echo: Why");

    view.change("Something else", false);
    expect(shown()).toBe("Something else");
  });

  it.each(STRICTNESS)(
    "hands on the text it starts with, nothing newer while the clock stands still, and then the newest$how",
    async ({ strict }) => {
      const clock = clockAt();
      const view = paced("Echo: ", true, { strict });
      expect(shown()).toBe("Echo: ");

      for (const text of ["Echo: Why ", "Echo: Why is ", "Echo: Why is the "]) {
        view.change(text, true);
        await aWhile();
      }
      expect(shown()).toBe("Echo: ");

      clock.advance(DRAWN_EVERY_MS);

      await waitFor(() => {
        expect(shown()).toBe("Echo: Why is the ");
      });
      // None of those in between.
      expect(texts()).toEqual(["Echo: ", "Echo: Why is the "]);
    },
  );

  it("hands on a newer text 50 ms after the one before, by the clock", async () => {
    // As the decision's record says (ADR 0016).
    expect(DRAWN_EVERY_MS).toBe(50);
    const clock = clockAt();
    const view = paced("Echo: ", true);
    view.change("Echo: Why", true);

    clock.advance(49);
    await aWhile();
    expect(shown()).toBe("Echo: ");

    clock.advance(1);
    await waitFor(() => {
      expect(shown()).toBe("Echo: Why");
    });
  });

  it.each(STRICTNESS)(
    "counts the wait from when the text before was on the page, not from when it was handed on$how",
    async ({ strict }) => {
      const clock = clockAt();
      // Putting "Echo: Why" on the page takes 30 ms by the clock, once.
      let slow = true;
      whileDrawn = (text) => {
        if (text === "Echo: Why" && slow) {
          slow = false;
          clock.advance(30);
        }
      };
      const view = paced("Echo: ", true, { strict });
      view.change("Echo: Why", true);
      clock.advance(DRAWN_EVERY_MS);
      // Handed on 50 ms in, and on the page at 80.
      await waitFor(() => {
        expect(shown()).toBe("Echo: Why");
      });

      view.change("Echo: Why is", true);
      // 100 ms in: 50 since it was handed on, 20 since it was on the page.
      clock.advance(20);
      await aWhile();
      expect(shown()).toBe("Echo: Why");

      clock.advance(30);
      await waitFor(() => {
        expect(shown()).toBe("Echo: Why is");
      });
    },
  );

  it.each(STRICTNESS)(
    "holds nothing back when the clock went backwards$how",
    async ({ strict }) => {
      const clock = clockAt();
      const view = paced("Echo: ", true, { strict });

      // Put back before a newer text comes: it is handed on at once.
      clock.advance(-1000);
      view.change("Echo: Why", true);
      expect(shown()).toBe("Echo: Why");

      // Put back while a newer text waits: it is handed on when the hook looks again.
      view.change("Echo: Why is", true);
      expect(shown()).toBe("Echo: Why");
      clock.advance(-1000);
      await waitFor(() => {
        expect(shown()).toBe("Echo: Why is");
      });
    },
  );

  it.each(STRICTNESS)(
    "hands on the whole text in the render in which it stops arriving$how",
    async ({ strict }) => {
      const whole = "Echo: Why is the sky blue?";
      clockAt();
      const view = paced("Echo: ", true, { strict });
      view.change("Echo: Why is the sky", true);
      await aWhile();

      view.change(whole, false);

      // At once, and in that very render: never the older text once it no longer
      // arrived.
      expect(shown()).toBe(whole);
      expect(handedOn.filter(({ arriving }) => !arriving)).toEqual([
        { text: whole, arriving: false },
      ]);
      expect(texts()).toEqual(["Echo: ", whole]);

      // Should it arrive again, it goes on from there, not from an older state.
      view.change(`${whole} It`, true);
      expect(shown()).toBe(whole);
    },
  );

  it("hands on a text that doesn't begin with what was last handed on as it is, at once", () => {
    clockAt();
    const view = paced("Echo: Why", true);

    view.change("Something else", true);

    expect(shown()).toBe("Something else");
  });

  it.each(STRICTNESS)("leaves no timer behind$how", ({ strict }) => {
    // The timers are faked here, and here alone, to count those the hook has set and
    // not yet cleared or run. Only the clock's: nothing in this test moves time.
    vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout"] });
    const view = paced("Echo: ", true, { strict });
    // Nothing waits.
    expect(vi.getTimerCount()).toBe(0);

    view.change("Echo: Why", true);
    expect(vi.getTimerCount()).toBe(1);

    // A newer text: the timer for the one before it is cleared.
    view.change("Echo: Why is", true);
    expect(vi.getTimerCount()).toBe(1);

    // It no longer arrives.
    view.change("Echo: Why is the sky blue?", false);
    expect(vi.getTimerCount()).toBe(0);

    // It arrives again, and leaves the page while a newer text waits.
    view.change("Echo: Why is the sky blue? It", true);
    expect(vi.getTimerCount()).toBe(1);
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe("an answer that arrives", () => {
  const TURNS = "POST /api/conversations/*/turns";
  const CONVERSATION = "GET /api/conversations/*";
  const CANCEL = "POST /api/conversations/*/turns/*/cancel";

  const ASKED = "Why is the sky blue?";
  // The recorded answer of `answer.sse`, as it arrives: "Echo: ", then a word at a
  // time. Its first three events (the turn's start, its step's, its text's) come
  // before.
  const WHOLE = "Echo: Why is the sky blue?";
  const BEFORE_THE_TEXT = 3;

  const drawing: {
    /** How many times the page fetched the code that draws markdown. */
    fetched: number;
    /** Each text the stand-in drew, once it was on the page: whether it was arriving
     * then, and whether the box to write in could send again. */
    drawn: { text: string; writing: boolean; sends: boolean }[];
  } = { fetched: 0, drawn: [] };

  /** Stands in for `./markdown`: shows the text as it is, and notes it each time React
   * has put a drawing of it on the page. */
  function StandIn({
    text,
    writing,
    className,
  }: {
    text: string;
    writing: boolean;
    className?: string;
  }) {
    useLayoutEffect(() => {
      drawing.drawn.push({
        text,
        writing,
        sends: screen.queryByRole("button", { name: "Send" }) !== null,
      });
    });
    return <p className={className}>{text}</p>;
  }

  beforeEach(() => {
    drawing.fetched = 0;
    drawing.drawn = [];
    // Every module is loaded anew by the next import, as on a page that was just
    // opened.
    vi.resetModules();
    vi.doMock("./markdown", () => {
      drawing.fetched += 1;
      return { default: StandIn };
    });
  });

  /** The texts the stand-in drew, in order, each once. */
  function drawnTexts(): string[] {
    return [...new Set(drawing.drawn.map(({ text }) => text))];
  }

  /** Open a new chat, and wait until the code that draws markdown is here: from then on
   * an answer is drawn from its first piece. */
  async function openWithRenderer() {
    const page = await import("@/tests/open");
    const user = page.open();
    await screen.findByLabelText("Message");
    await waitFor(() => {
      expect(drawing.fetched).toBe(1);
    });
    await act(async () => {
      await vi.dynamicImportSettled();
    });
    return { ...page, user };
  }

  /** The newest answer on the page. */
  function newestAnswer(): HTMLElement {
    const newest = screen.getAllByRole("article", { name: "Actio" }).at(-1);
    if (newest === undefined) {
      throw new Error("No answer is on the page.");
    }
    return newest;
  }

  it("is drawn from its first piece, then only from its newest once the clock has moved on, and whole when the box sends again", async () => {
    const clock = clockAt();
    const turns = sentByHand("answer.sse");
    fakeApi().on(TURNS, turns.answer);
    const { ask, user } = await openWithRenderer();

    await ask(user, ASKED);
    // Events sent at once reach the page in one render: the answer's words begin with
    // their first piece.
    turns.send(BEFORE_THE_TEXT + 1);
    await waitFor(() => {
      expect(drawnTexts()).toEqual(["Echo: "]);
    });
    // Its next four pieces, each a while apart.
    for (let piece = 0; piece < 4; piece++) {
      turns.send();
      await aWhile();
    }
    expect(drawnTexts()).toEqual(["Echo: "]);

    clock.advance(DRAWN_EVERY_MS);

    await waitFor(() => {
      expect(drawnTexts()).toEqual(["Echo: ", "Echo: Why is the sky "]);
    });
    // Its last piece, while the clock stands still again.
    turns.send();
    await aWhile();
    expect(drawnTexts()).toEqual(["Echo: ", "Echo: Why is the sky "]);

    turns.end();

    await screen.findByRole("button", { name: "Send" });
    // Whole in the render in which the box can send again, and not before.
    const ended = drawing.drawn.findIndex(({ sends }) => sends);
    expect(drawing.drawn[ended]).toEqual({
      text: WHOLE,
      writing: false,
      sends: true,
    });
    expect(
      drawing.drawn.slice(0, ended).filter(({ writing }) => !writing),
    ).toEqual([]);
    expect(newestAnswer().textContent).toBe(WHOLE);
  });

  it("has its reasoning drawn at the same pace, and whole once its words begin", async () => {
    // The recorded answer of `reasoning.sse`: a reasoning of four pieces, "Blue " the
    // first, then the words of `answer.sse`.
    const REASONING = "Blue light scatters most.";
    clockAt();
    const turns = sentByHand("reasoning.sse");
    fakeApi().on(TURNS, turns.answer);
    const { ask, user } = await openWithRenderer();

    await ask(user, ASKED);
    // The turn's start, its step's, its reasoning's, and its first piece.
    turns.send(4);
    await waitFor(() => {
      expect(drawnTexts()).toEqual(["Blue "]);
    });
    for (let piece = 0; piece < 3; piece++) {
      turns.send();
      await aWhile();
    }
    expect(drawnTexts()).toEqual(["Blue "]);

    // The reasoning's end, the words' start and their first piece, while the clock
    // still stands.
    turns.send(3);

    await waitFor(() => {
      expect(drawing.drawn).toContainEqual({
        text: REASONING,
        writing: false,
        sends: false,
      });
    });
    expect(drawing.drawn).toContainEqual({
      text: "Echo: ",
      writing: true,
      sends: false,
    });
    // Never whole while it was arriving.
    expect(
      drawing.drawn.filter(({ text }) => text === REASONING),
    ).not.toContainEqual(expect.objectContaining({ writing: true }));

    turns.end();

    await screen.findByRole("button", { name: "Send" });
    expect(drawing.drawn).toContainEqual({
      text: WHOLE,
      writing: false,
      sends: true,
    });
  });

  it("is what the server kept once it is stopped while a piece of it is held back", async () => {
    clockAt();
    const turns = sentByHand("answer.sse");
    const api = fakeApi();
    const asked = () =>
      (api.to(TURNS)[0]?.body as { message?: { id: string } } | undefined)
        ?.message?.id ?? "";
    const kept = (call: Call, by: Parameters<typeof turn>[0], text?: string) =>
      conversation(
        call.path.split("/")[3] ?? "",
        [
          question(asked(), ASKED),
          ...(text === undefined
            ? []
            : [answer(RECORDED.answer, text, asked(), turn(by))]),
        ],
        text === undefined ? turn(by) : null,
      );
    api
      .on(TURNS, turns.answer)
      .on(
        CONVERSATION,
        // The page has stopped reading. At the server the answer is written on.
        (call) =>
          json(200, kept(call, { status: "running", finish_reason: null })),
        (call) =>
          json(
            200,
            kept(call, { status: "stopped", finish_reason: null }, "Echo: Why"),
          ),
      )
      .on(CANCEL, () =>
        json(200, turn({ status: "stopped", finish_reason: null })),
      );
    const { ask, user } = await openWithRenderer();
    await ask(user, ASKED);
    turns.send(BEFORE_THE_TEXT + 1);
    await waitFor(() => {
      expect(drawnTexts()).toEqual(["Echo: "]);
    });
    // "Why ", held back while the clock stands still.
    turns.send();
    await aWhile();
    expect(drawnTexts()).toEqual(["Echo: "]);

    await user.click(screen.getByRole("button", { name: "Stop" }));

    expect(
      await screen.findByText(
        "This answer was stopped before it was finished.",
      ),
    ).toBeInTheDocument();
    expect(newestAnswer().textContent).toBe("Echo: Why");
    expect(drawing.drawn.at(-1)).toEqual({
      text: "Echo: Why",
      writing: false,
      sends: true,
    });
    expect(api.to(CANCEL)).toHaveLength(1);
  });
});
