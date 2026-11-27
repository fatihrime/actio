// A long stored conversation comes onto the page newest first: its newest messages at
// once, and its older ones behind them, in steps going back (ADR 0015, revision "a
// long conversation opens newest first"). Each step is drawn out of sight, as a
// transition, and shown, a step at a time, only while the reader is still. The page
// goes on meanwhile, and nothing on it is drawn again for it.
//
// The code that draws markdown is a stand-in here, which notes each text it drew once
// React has put it on the page, in order, with whether it was arriving: what React
// drew, and in which order, is certain from that, where a look at the page between
// two steps could only hope to catch one. A drawing React began and threw away is not
// noted. For one older answer the stand-in can wait for a promise the test keeps
// (`use`): the step that holds that answer can't land until then, so the older
// messages are held back for certain. The page holds that code for as long as it is
// open, so each test opens the app from modules loaded anew, as as-written.test.tsx
// does. So does the place where a reader left each conversation, which a conversation
// opens at when they come back to it: the tests of that lay the page out with sizes
// they make up (`layOut`).
import {
  act,
  cleanup,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import type { UserEvent } from "@testing-library/user-event";
import { Fragment, use, useLayoutEffect } from "react";
import { beforeEach, describe, expect, it, onTestFinished, vi } from "vitest";

import type { StoredMessage } from "@/lib/api/client";
import type * as Places from "@/lib/chat/places";
import { clockAt } from "@/tests/clock";
import {
  account,
  answer,
  conversation,
  fakeApi,
  GUEST,
  heldAfter,
  json,
  later,
  nothing,
  problem,
  question,
  sentByHand,
  streamed,
  turn,
} from "@/tests/fake-api";
import type { Call, FakeApi } from "@/tests/fake-api";
import { RECORDED } from "@/tests/recorded";

import { STILL_AFTER_MS } from "./follow";

const CONVERSATION = "GET /api/conversations/*";
const TURNS = "POST /api/conversations/*/turns";
const STREAM = "GET /api/conversations/*/turns/*/stream";
const SIGN_UP = "POST /api/account";

/** How a test asks a question: `ask` of the app's modules loaded anew. */
type Ask = (user: UserEvent, text: string) => Promise<void>;

/** The question each test asks, and its recorded answer (`answer.sse`). */
const ASKED = "Why is the sky blue?";
const ECHO = "Echo: Why is the sky blue?";

const drawing: {
  /** How many times the page fetched the code that draws markdown. */
  fetched: number;
  /** Each text the stand-in drew, once it was on the page, and whether it was
   * arriving then. */
  drawn: { text: string; writing: boolean }[];
  /** The text whose drawing waits, and what it waits for. */
  waits: { text: string; until: Promise<void> } | null;
} = { fetched: 0, drawn: [], waits: null };

/** Stands in for `./markdown`: shows the text as it is, and notes it each time React
 * has put a drawing of it on the page. A text of several paragraphs is drawn as
 * markdown is, a block for each inside one element, with what was between them in
 * between, so that its whole text is still the text. */
function StandIn({
  text,
  writing,
  className,
}: {
  text: string;
  writing: boolean;
  className?: string;
}) {
  if (drawing.waits?.text === text) {
    use(drawing.waits.until);
  }
  useLayoutEffect(() => {
    drawing.drawn.push({ text, writing });
  });
  const paragraphs = text.split("\n\n");
  return paragraphs.length === 1 ? (
    <p className={className}>{text}</p>
  ) : (
    <div className={className}>
      {paragraphs.map((paragraph, index) => (
        <Fragment key={index}>
          {index > 0 && "\n\n"}
          <p>{paragraph}</p>
        </Fragment>
      ))}
    </div>
  );
}

/** The texts the stand-in drew, in order. */
function drawnTexts(): string[] {
  return drawing.drawn.map(({ text }) => text);
}

/** The stored answers the stand-in drew, in order, as often as it drew each. */
function drawnAnswers(): string[] {
  return drawnTexts().filter((text) => text.startsWith("Answer "));
}

/** From now on, every module is loaded anew by the next import, as on a page that was
 * just opened, with the stand-in for the code that draws markdown. */
function aPageJustOpened(): void {
  drawing.fetched = 0;
  drawing.drawn = [];
  vi.resetModules();
  vi.doMock("./markdown", () => {
    drawing.fetched += 1;
    return { default: StandIn };
  });
}

/** Every listener the page puts on anything, from each test's start on: what was
 * listened for, and by what (`thePageListens`). */
let listeners: ReturnType<typeof noteListeners>;

function noteListeners() {
  return vi.spyOn(EventTarget.prototype, "addEventListener");
}

beforeEach(() => {
  drawing.waits = null;
  aPageJustOpened();
  listeners = noteListeners();
});

/** Keep the stand-in from drawing `text` until the test calls what this returns. A
 * test that doesn't call it leaves it waiting: its page is gone after the test. */
function holdDrawing(text: string): () => void {
  let arrive: () => void = () => undefined;
  const until = new Promise<void>((resolve) => {
    arrive = resolve;
  });
  drawing.waits = { text, until };
  onTestFinished(() => {
    arrive();
  });
  return arrive;
}

/** A stored conversation of `count` exchanges: "Question 0", "Answer 0", and so on,
 * each following the one before. */
function exchanges(count: number): StoredMessage[] {
  return Array.from({ length: count }, (_, index) => [
    question(
      `q${String(index)}`,
      `Question ${String(index)}`,
      index === 0 ? null : `a${String(index - 1)}`,
    ),
    answer(`a${String(index)}`, `Answer ${String(index)}`, `q${String(index)}`),
  ]).flat();
}

/** The answers "Answer `from`" up to, and without, "Answer `to`". */
function answers(from: number, to: number): string[] {
  return Array.from(
    { length: to - from },
    (_, index) => `Answer ${String(from + index)}`,
  );
}

/** What each of `messages` says. */
function textsOf(messages: StoredMessage[]): string[] {
  return messages.map((message) =>
    message.parts.map((part) => part.text).join(""),
  );
}

/** What each message on the page says, from the top. */
function onThePage(): string[] {
  return screen.queryAllByRole("article").map((message) => message.textContent);
}

function log(): HTMLElement {
  return screen.getByRole("log", { name: "Conversation" });
}

/** Wait until the page listens for its reader's scrolling. A conversation is on the
 * page a moment before the effect that listens has run (`useFollow`), and a test goes
 * on as soon as it sees the messages: a wheel turned in that moment, which no reader's
 * hand is fast enough for, was heard by nobody. The page then took its reader for
 * still and showed the older messages, and the test failed for it: three times in a
 * full run of the suites on a busy machine, in two tests of this file, and once in
 * thirty repeats inside the whole suite on a machine kept busy on purpose, where the
 * hook was made to say when it listened. */
async function thePageListens(): Promise<void> {
  const scrolling = log().parentElement;
  await waitFor(() => {
    expect(
      listeners.mock.calls.some(
        ([type], call) =>
          type === "wheel" && listeners.mock.contexts[call] === scrolling,
      ),
    ).toBe(true);
  });
}

/** From now on, note the first message on the page each time the page shows more. */
function watchShowing(): string[] {
  const firsts: string[] = [];
  const observer = new MutationObserver(() => {
    const first = onThePage()[0];
    if (first !== undefined && firsts.at(-1) !== first) {
      firsts.push(first);
    }
  });
  observer.observe(document.body, {
    subtree: true,
    attributes: true,
    attributeFilter: ["hidden"],
  });
  onTestFinished(() => {
    observer.disconnect();
  });
  return firsts;
}

/** What the page posted to start its first turn. */
function posted(api: FakeApi): {
  message?: { id: string };
  parent_id?: string;
} {
  const call: Call | undefined = api.to(TURNS)[0];
  expect(call, "no turn was requested").toBeDefined();
  return call?.body as { message?: { id: string }; parent_id?: string };
}

/** Give the page a moment, when nothing is to change: whatever it does with what
 * arrived takes it milliseconds. */
async function afterAMoment(): Promise<void> {
  await act(
    () =>
      new Promise<void>((resolve) => {
        setTimeout(resolve, 100);
      }),
  );
}

/** Open the conversation of `messages` by its address, as a page that has just been
 * loaded. The API answers with it once the page has had the code that draws markdown,
 * and a moment for the rest of what it asks for when it starts: so nothing else
 * arrives while the conversation comes onto the page. Later readings of it get
 * `afterwards`. */
async function openStored(
  api: FakeApi,
  messages: StoredMessage[],
  {
    strict = false,
    suspense = false,
    afterwards = [],
  }: {
    strict?: boolean;
    suspense?: boolean;
    afterwards?: Parameters<FakeApi["on"]>[1][];
  } = {},
) {
  const reading = later(() =>
    json(200, conversation(RECORDED.conversation, messages)),
  );
  api.on(CONVERSATION, reading.answer, ...afterwards);
  const page = await import("@/tests/open");
  const user = page.open(`/c/${RECORDED.conversation}`, { strict, suspense });
  await waitFor(() => {
    // Twice under StrictMode, which runs the effect that asks twice.
    expect(api.to(CONVERSATION).length).toBeGreaterThan(0);
    expect(drawing.fetched).toBe(1);
  });
  await act(async () => {
    await vi.dynamicImportSettled();
  });
  await afterAMoment();
  reading.release();
  return { ...page, user };
}

describe("a long stored conversation", () => {
  it.each([
    { how: "", strict: false },
    // React runs the effect that takes a step twice there: still one step.
    { how: " under React's StrictMode", strict: true },
  ])(
    "comes onto the page newest first, and its older messages behind, in steps going back$how",
    async ({ strict }) => {
      const messages = exchanges(25); // 50 messages
      await openStored(fakeApi(), messages, { strict });

      await waitFor(() => {
        expect(onThePage()).toEqual(textsOf(messages));
      });
      // The answers each step drew, in the order React first drew them: the newest
      // 10 messages, then the 20 before them, then the first 20.
      expect([...new Set(drawnTexts())]).toEqual([
        ...answers(20, 25),
        ...answers(10, 20),
        ...answers(0, 10),
      ]);
    },
  );

  it.each([
    { count: 25, said: ["off", null] },
    { count: 6, said: ["off", null] }, // two messages held back
    { count: 5, said: [null] }, // ten messages: whole at once
  ])(
    "has a screen reader say nothing of its older messages, from the moment it is on the page: $count exchanges",
    async ({ count, said }) => {
      // The log is a live region: what is added to it is said. Each value its
      // `aria-live` has had: the one it came onto the page with, which React gave it
      // before it put it there, then the one before each change, then its last.
      const before: (string | null)[] = [];
      let shown: Element | null = null;
      const observer = new MutationObserver((records) => {
        for (const record of records) {
          if (record.type === "attributes" && record.target === shown) {
            before.push(record.oldValue);
          }
          for (const node of record.addedNodes) {
            const found =
              node instanceof Element
                ? node.matches('[role="log"]')
                  ? node
                  : node.querySelector('[role="log"]')
                : null;
            if (found !== null) {
              shown = found;
            }
          }
        }
      });
      observer.observe(document.body, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ["aria-live"],
        attributeOldValue: true,
      });
      onTestFinished(() => {
        observer.disconnect();
      });
      const messages = exchanges(count);

      await openStored(fakeApi(), messages);

      await waitFor(() => {
        expect(onThePage()).toEqual(textsOf(messages));
      });
      await afterAMoment();
      expect([...before, log().getAttribute("aria-live")]).toEqual(said);
    },
  );

  it.each([
    {
      how: "a wheel turned",
      scrolls: () => {
        fireEvent.wheel(log().parentElement ?? log(), { deltaY: -100 });
      },
    },
    {
      how: "a key that scrolls",
      scrolls: () => {
        fireEvent.keyDown(document.body, { key: "PageUp" });
      },
    },
  ])(
    "keeps its older messages out of sight while the reader scrolls ($how), and shows them a step at a time once they are still",
    async ({ scrolls }) => {
      // The clock stands still: a reader who scrolled is never still until it moves.
      const clock = clockAt();
      const messages = exchanges(25);
      const newest = textsOf(messages.slice(40));
      // Nothing older is drawn before the reader has scrolled.
      const arrive = holdDrawing("Answer 10");
      await openStored(fakeApi(), messages);
      await waitFor(() => {
        expect(onThePage()).toEqual(newest);
      });
      await thePageListens();

      scrolls();
      arrive();

      // All of it is drawn, and none of it is shown, however long they scroll: what is
      // hidden isn't found by its role.
      await waitFor(() => {
        expect(drawnAnswers()).toHaveLength(25);
      });
      await act(
        () =>
          new Promise<void>((resolve) => {
            setTimeout(resolve, 4 * STILL_AFTER_MS);
          }),
      );
      expect(onThePage()).toEqual(newest);
      expect(log()).toHaveAttribute("aria-live", "off");
      const shownFrom = watchShowing();

      clock.advance(STILL_AFTER_MS);

      await waitFor(() => {
        expect(onThePage()).toEqual(textsOf(messages));
      });
      // A step at a time, the newest first: the 20 messages before the newest, then
      // the first 20.
      expect(shownFrom).toEqual(["Question 10", "Question 0"]);
      // Drawn once each, showing them included.
      expect(new Set(drawnAnswers())).toEqual(new Set(answers(0, 25)));
      expect(drawnAnswers()).toHaveLength(25);
      expect(log()).not.toHaveAttribute("aria-live");
    },
  );

  it("draws each answer once, and none again for showing it, typing, a question and its answer", async () => {
    const messages = exchanges(25);
    const api = fakeApi().on(TURNS, () => streamed("answer.sse"));
    const { ask, user } = await openStored(api, messages);
    await waitFor(() => {
      expect(onThePage()).toEqual(textsOf(messages));
    });
    await afterAMoment();

    // All shown by now, and each drawn once.
    expect(drawnAnswers()).toHaveLength(25);
    expect(new Set(drawnAnswers())).toEqual(new Set(answers(0, 25)));

    await ask(user, ASKED);

    expect(await screen.findByText(ECHO)).toBeInTheDocument();
    await screen.findByRole("button", { name: "Send" });
    expect(drawnAnswers()).toHaveLength(25);
  });

  it.each<{
    how: string;
    last: StoredMessage;
    asks: (user: UserEvent, ask: Ask) => Promise<void>;
    left: number;
    came: number;
  }>([
    {
      how: "the answer to a question asked",
      // The conversation's last answer is whole.
      last: answer("a24", "Answer 24", "q24"),
      asks: async (user, ask) => {
        await ask(user, ASKED);
      },
      // The newest 10 stay, and the question and its answer come after them.
      left: 10,
      came: 2,
    },
    {
      how: "an answer tried again",
      // Its last answer was stopped. Tried again, the new one takes its place, among
      // the messages the conversation came with.
      last: answer(
        "a24",
        "Answer 24",
        "q24",
        turn({ status: "stopped", finish_reason: null }),
      ),
      asks: async (user) => {
        await user.click(screen.getByRole("button", { name: "Try again" }));
      },
      left: 9,
      came: 1,
    },
  ])(
    "draws $how, while its older messages aren't all shown, as arriving",
    async ({ last, asks, left, came }) => {
      // The reader scrolls, and the clock stands still: nothing older is shown.
      clockAt();
      const messages = [...exchanges(25).slice(0, -1), last];
      const newest = textsOf(messages.slice(40));
      // Nothing older is drawn before the reader has scrolled. All of it is drawn
      // before anything is typed or pressed: a drawing that waits while React answers
      // an event inside the test's `act` is left there, and the page stands still.
      const arrive = holdDrawing("Answer 10");
      // The answer's first words, and then nothing more while the test looks.
      const api = fakeApi().on(TURNS, heldAfter("answer.sse", 5));
      const { ask, user } = await openStored(api, messages);
      await waitFor(() => {
        expect(onThePage()).toEqual(newest);
      });
      await thePageListens();
      fireEvent.wheel(log().parentElement ?? log(), { deltaY: -100 });
      arrive();
      await waitFor(() => {
        expect(drawnAnswers()).toHaveLength(25);
      });

      await asks(user, ask);

      expect(await screen.findByText(/^Echo: Why/)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();
      // The newest that were shown, then what came: none older.
      expect(onThePage().slice(0, left)).toEqual(newest.slice(0, left));
      expect(onThePage()).toHaveLength(left + came);
      const arriving = drawing.drawn.filter(({ text }) =>
        text.startsWith("Echo: "),
      );
      expect(arriving).not.toEqual([]);
      expect(arriving.filter(({ writing }) => !writing)).toEqual([]);

      // Another conversation ends the reading while the page is still there.
      await user.click(screen.getByRole("button", { name: "New chat" }));
      expect(
        await screen.findByText("What would you like to know?"),
      ).toBeInTheDocument();
    },
  );

  it("takes a question while its older messages are held back, and still brings them all", async () => {
    const messages = exchanges(25);
    const newest = textsOf(messages.slice(40));
    // An answer of the first step back: that step can't land until it is drawn.
    const arrive = holdDrawing("Answer 10");
    const api = fakeApi().on(TURNS, () => streamed("answer.sse"));
    const { ask, user, SUSPENDED } = await openStored(api, messages, {
      suspense: true,
    });
    await waitFor(() => {
      expect(onThePage()).toEqual(newest);
    });
    await afterAMoment();
    // Held back, and the page as it was: a step that waits is a transition, which
    // leaves the page alone. Any other update that waited would have put the
    // boundary's fallback in its place.
    expect(onThePage()).toEqual(newest);
    expect(screen.queryByText(SUSPENDED)).not.toBeInTheDocument();
    expect(log()).toHaveAttribute("aria-live", "off");

    await ask(user, ASKED);

    expect(await screen.findByText(ECHO)).toBeInTheDocument();
    await screen.findByRole("button", { name: "Send" });
    // It follows the conversation's last message, whether or not all are drawn.
    expect(posted(api).parent_id).toBe("a24");
    expect(onThePage()).toEqual([...newest, ASKED, ECHO]);
    expect(log()).toHaveAttribute("aria-live", "off");
    expect(screen.queryByText(SUSPENDED)).not.toBeInTheDocument();

    arrive();

    await waitFor(() => {
      expect(onThePage()).toEqual([...textsOf(messages), ASKED, ECHO]);
    });
    expect(log()).not.toHaveAttribute("aria-live");
    expect(screen.queryByText(SUSPENDED)).not.toBeInTheDocument();
  });

  it("is shown whole when it is read again while its older messages are held back", async () => {
    const messages = exchanges(25);
    const newest = textsOf(messages.slice(40));
    const arrive = holdDrawing("Answer 10");
    const api = fakeApi().on(TURNS, () => streamed("model_unavailable.sse"));
    const { ask, user, SUSPENDED } = await openStored(api, messages, {
      suspense: true,
      // The model failed, so the page reads the conversation again: it holds the
      // question now, and the turn that failed to answer it. Here the conversation
      // says something else of that turn than its stream did, which a server never
      // would: the answer the page held for the failed turn had nothing in it and
      // isn't drawn, so nothing else on the page tells what was read again from
      // what the stream left.
      afterwards: [
        () =>
          json(
            200,
            conversation(
              RECORDED.conversation,
              [
                ...messages,
                question(posted(api).message?.id ?? "", ASKED, "a24"),
              ],
              turn({ status: "failed", error: "model_missing" }),
            ),
          ),
      ],
    });
    await waitFor(() => {
      expect(onThePage()).toEqual(newest);
    });

    await ask(user, ASKED);

    // As the server has it, still from the same message on.
    expect(
      await screen.findByText("The model isn't on its server."),
    ).toBeInTheDocument();
    expect(onThePage()).toEqual([...newest, ASKED]);
    expect(api.to(CONVERSATION)).toHaveLength(2);
    expect(screen.queryByText(SUSPENDED)).not.toBeInTheDocument();

    arrive();

    await waitFor(() => {
      expect(onThePage()).toEqual([...textsOf(messages), ASKED]);
    });
    expect(log()).not.toHaveAttribute("aria-live");
    expect(screen.queryByText(SUSPENDED)).not.toBeInTheDocument();
  });

  it("holds nothing back once it is gone when it is read again", async () => {
    const messages = exchanges(25);
    // Never drawn: it is gone before its step could land.
    holdDrawing("Answer 10");
    const api = fakeApi().on(
      TURNS,
      () => streamed("model_unavailable.sse"),
      () => streamed("answer.sse"),
    );
    const { ask, user, SUSPENDED } = await openStored(api, messages, {
      suspense: true,
      afterwards: [() => problem(404, "No such conversation.")],
    });
    await waitFor(() => {
      expect(onThePage()).toEqual(textsOf(messages.slice(40)));
    });

    await ask(user, ASKED);

    expect(
      await screen.findByText("What would you like to know?"),
    ).toBeInTheDocument();
    expect(onThePage()).toEqual([]);
    // At once, and not when the steps have run down: the drawing that waits is
    // still waiting.
    expect(log()).not.toHaveAttribute("aria-live");
    expect(screen.queryByText(SUSPENDED)).not.toBeInTheDocument();

    // The question that found no conversation is back in the box. Sent again, it is
    // the first message of a new conversation, at the place in the list where a step
    // was held back. It is shown as it comes.
    expect(screen.getByLabelText("Message")).toHaveValue(ASKED);
    await user.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByText(ECHO)).toBeInTheDocument();
    expect(onThePage()).toEqual([ASKED, ECHO]);
    expect(screen.queryByText(SUSPENDED)).not.toBeInTheDocument();
  });
});

describe("a new conversation", () => {
  it("has no step of its own: its first message is the first thing in the log", async () => {
    // A step of which nothing is there has no element. An empty one would stand in
    // the log as one more thing, and the log's gap would follow it: every new
    // conversation would begin a little lower.
    fakeApi().on(TURNS, () => streamed("answer.sse"));
    const page = await import("@/tests/open");
    const user = page.open();

    await page.ask(user, ASKED);

    expect(await screen.findByText(ECHO)).toBeInTheDocument();
    expect(log().firstElementChild).toBe(
      screen.getByRole("article", { name: "You" }),
    );
  });
});

// A stored conversation that its reader left away from its end opens where they left
// it: at the block of a message at the top of the screen, with the top of the screen
// as far below the block's top as it was. The messages around that place are drawn at
// once, and the rest in steps behind them, the nearest first, below and above in turn.
// Those above are shown while the reader is still, as for a conversation that opens at
// its end; those below as soon as they are drawn. The page is laid out with sizes the
// test makes up (`layOut`), and the reader comes back with the browser's Back unless a
// test says otherwise.

/** On a page that `layOut` lays out: how tall a message that is shown is, unless it is
 * drawn in blocks, and the room for the conversation on the screen, in pixels. */
const TALL = 100;
const ROOM = 500;

/** How tall a block of a message drawn in several is, in pixels: a paragraph, and a
 * block that is smaller until it has been near the window, and then has its real
 * height, for as long as its element lives. That is what the markdown library does
 * with code, `content-visibility: auto`, which a rule of the stylesheet undoes
 * (app/globals.css), and what the page did before it. It stands here for whatever in
 * a message above the place can be another height when the conversation is drawn
 * anew: a diagram, which is drawn later, and code wherever that rule wouldn't hold. */
const PARAGRAPH = 50;
const CODE_UNSEEN = 218;
const CODE_SEEN = 352;

/** The messages the log shows, from the top: none of those drawn out of sight. */
function shownMessages(): HTMLElement[] {
  return [
    ...document.querySelectorAll<HTMLElement>('[role="log"] article'),
  ].filter((message) => message.closest("[hidden]") === null);
}

/** The blocks a message is drawn in, as the stand-in draws a text of several
 * paragraphs: none for one drawn otherwise. */
function blocksOf(message: Element): Element[] {
  return [...message.querySelectorAll(":scope > div > p")];
}

/** From now on, the page is laid out as a browser would lay it out, with sizes made up:
 * the messages that are shown stand one under another from the top of the
 * conversation, each `TALL` pixels, or as high as its blocks, of which one that begins
 * with "Code" is code. The element that scrolls has `room` for them, until the test
 * gives it another (`windowOf`), as when the window is made larger. Nothing else has a
 * size. The page is told that a size changed only when the test calls `resized`, as a
 * browser tells it once it has laid the page out; and code has been near the window
 * when the test calls `painted`, as a browser draws a frame. */
function layOut({ room: first = ROOM }: { room?: number } = {}): {
  resized: () => void;
  painted: () => void;
  windowOf: (to: number) => void;
} {
  let room = first;
  const scrolledTo = new WeakMap<Element, number>();
  // The blocks of code that have been near the window.
  const seen = new WeakSet<Element>();
  const isScroller = (element: Element) =>
    element.firstElementChild?.getAttribute("role") === "log";
  const tallness = (block: Element) =>
    block.textContent.startsWith("Code")
      ? seen.has(block)
        ? CODE_SEEN
        : CODE_UNSEEN
      : PARAGRAPH;
  const heightOf = (message: Element) => {
    const blocks = blocksOf(message);
    return blocks.length === 0
      ? TALL
      : blocks.reduce((sum, block) => sum + tallness(block), 0);
  };
  const height = () =>
    Math.max(
      room,
      shownMessages().reduce((sum, message) => sum + heightOf(message), 0),
    );
  // A browser never scrolls past the end, nor above the top.
  const reachable = (top: number) =>
    Math.max(0, Math.min(top, height() - room));
  // How far down `element` is: how high the messages shown before it are.
  const down = (element: Element) =>
    shownMessages()
      .filter(
        (message) =>
          element.compareDocumentPosition(message) &
          Node.DOCUMENT_POSITION_PRECEDING,
      )
      .reduce((sum, message) => sum + heightOf(message), 0);
  // Where `element` is in the conversation, from its top, and how high: a message
  // that is shown, what holds its blocks, or one of them. Nothing for anything else.
  const placeOf = (element: Element): { top: number; high: number } | null => {
    const message = element.closest("article");
    if (message === null || !shownMessages().some((one) => one === message)) {
      return null;
    }
    const blocks = blocksOf(message);
    if (element === message || element === blocks[0]?.parentElement) {
      return { top: down(message), high: heightOf(message) };
    }
    const at = blocks.indexOf(element);
    return at === -1
      ? null
      : {
          top:
            down(message) +
            blocks
              .slice(0, at)
              .reduce((sum, block) => sum + tallness(block), 0),
          high: tallness(element),
        };
  };

  const scrollTop = Object.getOwnPropertyDescriptor(
    Element.prototype,
    "scrollTop",
  );
  const scrollHeight = Object.getOwnPropertyDescriptor(
    Element.prototype,
    "scrollHeight",
  );
  const clientHeight = Object.getOwnPropertyDescriptor(
    Element.prototype,
    "clientHeight",
  );
  const offsetTop = Object.getOwnPropertyDescriptor(
    HTMLElement.prototype,
    "offsetTop",
  );
  vi.spyOn(Element.prototype, "scrollTop", "get").mockImplementation(function (
    this: Element,
  ) {
    return isScroller(this)
      ? reachable(scrolledTo.get(this) ?? 0)
      : (scrollTop?.get?.call(this) as number);
  });
  vi.spyOn(Element.prototype, "scrollTop", "set").mockImplementation(function (
    this: Element,
    to: number,
  ) {
    if (isScroller(this)) {
      scrolledTo.set(this, reachable(to));
    } else {
      scrollTop?.set?.call(this, to);
    }
  });
  vi.spyOn(Element.prototype, "scrollHeight", "get").mockImplementation(
    function (this: Element) {
      return isScroller(this)
        ? height()
        : (scrollHeight?.get?.call(this) as number);
    },
  );
  vi.spyOn(Element.prototype, "clientHeight", "get").mockImplementation(
    function (this: Element) {
      return isScroller(this)
        ? room
        : (clientHeight?.get?.call(this) as number);
    },
  );
  vi.spyOn(HTMLElement.prototype, "offsetTop", "get").mockImplementation(
    function (this: HTMLElement) {
      return this.parentElement?.getAttribute("role") === "log"
        ? down(this)
        : (offsetTop?.get?.call(this) as number);
    },
  );
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(
    function (this: Element) {
      if (isScroller(this)) {
        return new DOMRect(0, 0, 800, room);
      }
      const scrolling = this.closest('[role="log"]')?.parentElement;
      const placed = placeOf(this);
      // As the simulated page has it, for anything else: nothing has a size.
      return scrolling === null || scrolling === undefined || placed === null
        ? new DOMRect()
        : new DOMRect(0, placed.top - scrolling.scrollTop, 800, placed.high);
    },
  );

  // Told of every observer the page makes, to tell them that a size changed.
  const observers = new Set<() => void>();
  vi.stubGlobal(
    "ResizeObserver",
    class {
      readonly #changed: () => void;
      constructor(changed: () => void) {
        this.#changed = changed;
        observers.add(changed);
      }
      observe() {
        // Sizes change when the test says so.
      }
      unobserve() {
        // Nothing to let go of.
      }
      disconnect() {
        observers.delete(this.#changed);
      }
    },
  );
  return {
    resized: () => {
      act(() => {
        for (const changed of observers) {
          changed();
        }
      });
    },
    windowOf: (to) => {
      room = to;
    },
    painted: () => {
      for (const message of shownMessages()) {
        for (const block of blocksOf(message)) {
          const box = block.getBoundingClientRect();
          if (box.bottom > 0 && box.top < room) {
            seen.add(block);
          }
        }
      }
    },
  };
}

/** The element that scrolls the conversation. */
function scroller(): HTMLElement {
  const found = log().parentElement;
  if (found === null) {
    throw new Error("The conversation has no element that scrolls it.");
  }
  return found;
}

/** The reader scrolls to `top`, and the browser says so. */
function scrollTo(top: number): void {
  const scrolling = scroller();
  act(() => {
    scrolling.scrollTop = top;
    fireEvent.scroll(scrolling);
  });
}

/** How far below the top of message `id` the top of the screen is, in pixels: nothing
 * while it isn't shown. */
function offsetOf(id: string): number | null {
  const message = shownMessages().find(
    (shown) => shown.dataset.messageId === id,
  );
  return message === undefined
    ? null
    : scroller().getBoundingClientRect().top -
        message.getBoundingClientRect().top;
}

/** How far the reader is from the end of the conversation, in pixels. */
function belowTheReader(): number {
  const scrolling = scroller();
  return scrolling.scrollHeight - scrolling.scrollTop - scrolling.clientHeight;
}

/** From now on, note what `look` sees at each change of the page, once it sees
 * anything: whatever moves the reader does so in the task that changes the page, so
 * this is what a frame after it would show. */
function watch<T>(look: () => T | null): T[] {
  const seen: T[] = [];
  const observer = new MutationObserver(() => {
    const now = look();
    if (now !== null) {
      seen.push(now);
    }
  });
  observer.observe(document.body, {
    subtree: true,
    childList: true,
    attributes: true,
    attributeFilter: ["hidden"],
  });
  onTestFinished(() => {
    observer.disconnect();
  });
  return seen;
}

/** Wait for the next frame. */
async function aFrame(): Promise<void> {
  await act(
    () =>
      new Promise<void>((resolve) => {
        requestAnimationFrame(() => {
          resolve();
        });
      }),
  );
}

/** Where each test's reader leaves a conversation of 40 exchanges: at "Answer 20", its
 * 42nd message, with the top of the screen 30 pixels below the answer's top. The
 * messages drawn at once when it opens there again begin with its 40th: 2 before the
 * place, and 8 from it on. With the step below them, they end before its 70th. */
const PLACE = { message: "a20", offset: 30 };
const PLACED = 41;
const FIRST = 39;
const STEP_BELOW_ENDS = 69;

/** What each step draws, when a conversation of 40 exchanges opens at `PLACE`: the
 * answers drawn at once, then those of the step below them, of the step above them, of
 * the next step below, and of the next above. */
const AROUND_THE_PLACE = [
  ...answers(19, 24),
  ...answers(24, 34),
  ...answers(9, 19),
  ...answers(34, 40),
  ...answers(0, 9),
];

/** The conversation of `messages`, as the API answers it. */
function stored(messages: StoredMessage[]): () => Response {
  return () => json(200, conversation(RECORDED.conversation, messages));
}

/** Open the conversation of `messages`, as `openStored` does, wait until all of it is
 * shown, and go to `PLACE`, as its reader would. */
async function openAndGoToThePlace(
  api: FakeApi,
  messages: StoredMessage[],
  options: Parameters<typeof openStored>[2] = {},
) {
  const page = await openStored(api, messages, options);
  await waitFor(() => {
    expect(onThePage()).toEqual(textsOf(messages));
  });
  scrollTo(PLACED * TALL + PLACE.offset);
  expect(offsetOf(PLACE.message)).toBe(PLACE.offset);
  return page;
}

/** Leave the conversation for a new chat, and start noting what is drawn anew. */
async function leave(user: UserEvent): Promise<void> {
  await user.click(screen.getByRole("button", { name: "New chat" }));
  await screen.findByText("What would you like to know?");
  drawing.drawn = [];
}

// Safari's engine works out anew the style of everything inside an element when that
// element stops being the last one in its parent, or starts. So nothing is added after
// an element that holds much, or taken away from after it: what comes and goes there is
// on the page from the first, and what a conversation gets comes before its log's last
// element (ADR 0015, revision "nothing is added after an element that holds much").
describe("an element that holds much is never the last in its parent", () => {
  it("has the way to the newest message on the page from the first, only displayed while the reader is away from the end: nothing is added beside the conversation, or taken away, as they leave the end and come back", async () => {
    layOut();
    const messages = exchanges(40);
    await openStored(fakeApi(), messages);
    await waitFor(() => {
      expect(onThePage()).toEqual(textsOf(messages));
    });
    const way = () =>
      screen.queryByRole("button", { name: "Go to the newest message" });
    const beside = scroller().parentElement;
    const button = document.querySelector(
      'button[aria-label="Go to the newest message"]',
    );
    // At the end: there, and not displayed, which also keeps it from the keyboard and
    // from a screen reader.
    expect(button).not.toBeNull();
    expect(button?.parentElement).toBe(beside);
    expect(button).not.toBeVisible();
    expect(way()).not.toBeInTheDocument();
    // And the element that scrolls is not the last one in its parent.
    expect(beside?.lastElementChild).not.toBe(scroller());
    expect(beside?.lastElementChild).not.toBeNull();
    const changes: MutationRecord[] = [];
    const watching = new MutationObserver((records) => {
      changes.push(...records);
    });
    onTestFinished(() => {
      watching.disconnect();
    });
    if (beside !== null) {
      watching.observe(beside, { childList: true });
    }

    scrollTo(10 * TALL);

    // Away from the end: the same element, displayed now.
    expect(way()).toBe(button);
    expect(button).toBeVisible();

    scrollTo(scroller().scrollHeight - ROOM);

    expect(belowTheReader()).toBe(0);
    expect(way()).not.toBeInTheDocument();
    expect(button).not.toBeVisible();
    expect([...changes, ...watching.takeRecords()]).toEqual([]);
  });

  it.each([
    { which: "a stored conversation", stored: true },
    { which: "a new one", stored: false },
  ])(
    "ends the log of $which with an element that is never displayed: a question, its answer and what is said of them come before it",
    async ({ stored: isStored }) => {
      const messages = isStored ? exchanges(25) : [];
      const api = fakeApi().on(TURNS, () => streamed("answer.sse"));
      const page = await import("@/tests/open");
      const user = isStored
        ? (await openStored(api, messages)).user
        : page.open();
      await screen.findByRole("log", { name: "Conversation" });
      await waitFor(() => {
        expect(onThePage()).toEqual(textsOf(messages));
      });
      const end = log().lastElementChild;
      expect(end).not.toBeNull();
      expect(end).not.toBeVisible();
      expect(end?.textContent).toBe("");
      // Everything added to the log itself from here on, with what it was put before.
      const added: { node: Node; before: Node | null }[] = [];
      const note = (records: MutationRecord[]) => {
        for (const { addedNodes, nextSibling } of records) {
          for (const node of addedNodes) {
            added.push({ node, before: nextSibling });
          }
        }
      };
      const watching = new MutationObserver(note);
      onTestFinished(() => {
        watching.disconnect();
      });
      watching.observe(log(), { childList: true });
      const lines = () =>
        screen.queryAllByRole("list", { name: "What this answer took" }).length;
      const stood = lines();

      await page.ask(user, ASKED);

      expect(await screen.findByText(ECHO)).toBeInTheDocument();
      await screen.findByRole("button", { name: "Send" });
      // What the answer took comes a frame after its last words.
      await waitFor(() => {
        expect(lines()).toBe(stood + 1);
      });
      note(watching.takeRecords());
      // The control: the question and its answer were added to the log itself.
      expect(added.length).toBeGreaterThan(1);
      expect(added.filter(({ before }) => before === null)).toEqual([]);
      expect(log().lastElementChild).toBe(end);
      expect(end).not.toBeVisible();
    },
  );

  it("ends the log with that element from the first, also while messages are still held back below what is shown", async () => {
    layOut();
    const messages = exchanges(40);
    const { user } = await openAndGoToThePlace(fakeApi(), messages, {
      afterwards: [stored(messages)],
    });
    await leave(user);
    // At each change of the page while the conversation comes in around the place: how
    // many of its messages are shown, and what its log ends with.
    const ends = watch(() => {
      const end = document.querySelector('[role="log"]')?.lastElementChild;
      return end === null || end === undefined || shownMessages().length === 0
        ? null
        : {
            shown: shownMessages().length,
            end:
              end instanceof HTMLElement && end.hidden && end.textContent === ""
                ? "not displayed, and empty"
                : end.textContent.slice(0, 40),
          };
    });

    window.history.back();

    await waitFor(() => {
      expect(onThePage()).toEqual(textsOf(messages));
    });
    // The control: it opened with messages held back below, which came in steps.
    expect(ends[0]?.shown).toBe(10);
    expect(new Set(ends.map(({ shown }) => shown)).size).toBeGreaterThan(2);
    expect(
      ends.filter(({ end }) => end !== "not displayed, and empty"),
    ).toEqual([]);
  });
});

describe("a long stored conversation its reader left away from its end", () => {
  it.each([
    { how: "", strict: false },
    // React runs the effects that go to the place and take a step twice there: still
    // the place, and one step.
    { how: " under React's StrictMode", strict: true },
  ])(
    "opens where they left it, the messages around it first, and the rest in steps behind, the nearest first, below and above in turn$how",
    async ({ strict }) => {
      layOut();
      const messages = exchanges(40); // 80 messages
      const { user } = await openAndGoToThePlace(fakeApi(), messages, {
        strict,
        afterwards: [stored(messages)],
      });
      await leave(user);
      const seen = watch(() => {
        const offset = offsetOf(PLACE.message);
        return offset === null
          ? null
          : {
              offset,
              messages: shownMessages().length,
              live: log().getAttribute("aria-live"),
            };
      });

      window.history.back();

      await waitFor(() => {
        expect(onThePage()).toEqual(textsOf(messages));
      });
      expect([...new Set(drawnAnswers())]).toEqual(AROUND_THE_PLACE);
      // From the moment it is on the page, with the 10 around the place, the place
      // stands where it stood, and a screen reader is told nothing of what comes.
      expect(seen[0]).toEqual({
        offset: PLACE.offset,
        messages: 10,
        live: "off",
      });
      expect(seen.filter(({ offset }) => offset !== PLACE.offset)).toEqual([]);
      expect(
        seen.filter(({ messages, live }) => messages < 80 && live !== "off"),
      ).toEqual([]);
      expect(log()).not.toHaveAttribute("aria-live");
      expect(offsetOf(PLACE.message)).toBe(PLACE.offset);
      expect(
        screen.getByRole("button", { name: "Go to the newest message" }),
      ).toBeVisible();
    },
  );

  it("draws each of its answers once when it opens there", async () => {
    layOut();
    const messages = exchanges(40);
    const { user } = await openAndGoToThePlace(fakeApi(), messages, {
      afterwards: [stored(messages)],
    });
    await leave(user);

    window.history.back();

    await waitFor(() => {
      expect(onThePage()).toEqual(textsOf(messages));
    });
    await afterAMoment();
    expect(drawnAnswers()).toEqual(AROUND_THE_PLACE);
  });

  it.each([
    {
      how: "ten that fill the window, and no more",
      room: ROOM,
      first: 10,
    },
    {
      how: "and in a window ten don't fill, as many steps more as fill it, before anything is shown, and no more",
      room: 2500,
      // 10 and 20 are 3,000 pixels.
      first: 30,
    },
  ])(
    "opens at its end with its newest messages at once: $how",
    async ({ room, first }) => {
      layOut({ room });
      const messages = exchanges(40);
      const seen = watch(() =>
        shownMessages().length === 0
          ? null
          : { messages: shownMessages().length, below: belowTheReader() },
      );

      await openStored(fakeApi(), messages);

      await waitFor(() => {
        expect(onThePage()).toEqual(textsOf(messages));
      });
      // At the first change a reader could see, with the reader at the end.
      expect(seen[0]).toEqual({ messages: first, below: 0 });
      expect([...new Set(drawnAnswers())]).toEqual([
        ...answers(35, 40),
        ...answers(25, 35),
        ...answers(15, 25),
        ...answers(5, 15),
        ...answers(0, 5),
      ]);
    },
  );

  it("opens where its reader left it when the page is loaded again in the same tab", async () => {
    layOut();
    const messages = exchanges(40);
    const { user } = await openAndGoToThePlace(fakeApi(), messages, {
      afterwards: [stored(messages)],
    });
    await leave(user);
    // The page is left: what it keeps for the tab is written then.
    window.dispatchEvent(new Event("pagehide"));
    cleanup();
    // Loaded again: the app's modules anew, as on a page that was just opened.
    aPageJustOpened();
    const seen = watch(() => offsetOf(PLACE.message));

    await openStored(fakeApi(), messages);

    await waitFor(() => {
      expect(onThePage()).toEqual(textsOf(messages));
    });
    expect(seen[0]).toBe(PLACE.offset);
    expect(seen.filter((offset) => offset !== PLACE.offset)).toEqual([]);
    expect([...new Set(drawnAnswers())]).toEqual(AROUND_THE_PLACE);
  });

  it("opens at its end when its reader left it there, and follows an answer that arrives", async () => {
    const { resized } = layOut();
    const messages = exchanges(25);
    const api = fakeApi().on(STREAM, () => streamed("answer.sse"));
    const { user } = await openStored(api, messages, {
      // Another window asked a question meanwhile, whose answer is being written.
      afterwards: [
        () =>
          json(
            200,
            conversation(
              RECORDED.conversation,
              [...messages, question("q25", "Question 25", "a24")],
              turn({ status: "running", finish_reason: null, credits: null }),
            ),
          ),
      ],
    });
    await waitFor(() => {
      expect(onThePage()).toEqual(textsOf(messages));
    });
    // Up, where the page notes where they are, and back down to the end.
    scrollTo(700);
    await aFrame();
    scrollTo(scroller().scrollHeight - ROOM - 20);
    await leave(user);
    const below = watch(() =>
      shownMessages().length === 0 ? null : belowTheReader(),
    );

    window.history.back();

    await waitFor(() => {
      expect(onThePage()).toEqual([...textsOf(messages), "Question 25", ECHO]);
    });
    // At its end from the moment it is on the page, newest first.
    expect(below[0]).toBe(0);
    expect([...new Set(drawnAnswers())]).toEqual([
      ...answers(20, 25),
      ...answers(10, 20),
      ...answers(0, 10),
    ]);
    // The answer came at the end, and the page goes on to it once it is laid out.
    resized();
    expect(belowTheReader()).toBe(0);
    expect(
      screen.queryByRole("button", { name: "Go to the newest message" }),
    ).not.toBeInTheDocument();
  });

  it("opens at its end when the message of its place is no longer in it", async () => {
    layOut();
    const messages = exchanges(40);
    // Its reader went on in another window from an earlier version of that answer.
    const changed = messages.map((message) =>
      message.id === PLACE.message ? { ...message, id: "a20-again" } : message,
    );
    const { user } = await openAndGoToThePlace(fakeApi(), messages, {
      afterwards: [stored(changed)],
    });
    await leave(user);
    const below = watch(() =>
      shownMessages().length === 0 ? null : belowTheReader(),
    );

    window.history.back();

    await waitFor(() => {
      expect(onThePage()).toEqual(textsOf(changed));
    });
    // As without a place: its newest 10 first, at its end, then the older ones.
    expect([...new Set(drawnAnswers())]).toEqual([
      ...answers(35, 40),
      ...answers(25, 35),
      ...answers(15, 25),
      ...answers(5, 15),
      ...answers(0, 5),
    ]);
    expect(below.filter((by) => by !== 0)).toEqual([]);
  });

  it.each([
    {
      how: "after its reader went to the form for signing up and back",
      signsUp: false,
      drawn: AROUND_THE_PLACE,
      at: PLACE.offset,
    },
    {
      how: "at its end once the session changed hands",
      signsUp: true,
      // As without a place: its newest first.
      drawn: [
        ...answers(35, 40),
        ...answers(25, 35),
        ...answers(15, 25),
        ...answers(5, 15),
        ...answers(0, 5),
      ],
      // At the end, the place's message is far above the top of the screen.
      at: 80 * TALL - ROOM - PLACED * TALL,
    },
  ])("opens $how", async ({ signsUp, drawn, at }) => {
    layOut();
    const messages = exchanges(40);
    // The same person as the guest was, with a session of their own now.
    const api = fakeApi().on(SIGN_UP, () =>
      json(201, account({ id: GUEST.id })),
    );
    const { user } = await openAndGoToThePlace(api, messages, {
      afterwards: [stored(messages)],
    });
    await user.click(
      within(screen.getByRole("banner")).getByRole("link", { name: "Sign up" }),
    );
    await screen.findByRole("heading", { name: "Create an account" });
    drawing.drawn = [];

    if (signsUp) {
      await user.type(
        screen.getByLabelText("Email address"),
        "ada@example.com",
      );
      await user.type(
        screen.getByLabelText("Password", { exact: true }),
        "correct horse battery",
      );
      await user.click(screen.getByRole("button", { name: "Sign up" }));
    } else {
      window.history.back();
    }

    await waitFor(() => {
      expect(onThePage()).toEqual(textsOf(messages));
    });
    expect([...new Set(drawnAnswers())]).toEqual(drawn);
    // How far the top of the screen is below the place's message.
    expect(offsetOf(PLACE.message)).toBe(at);
  });

  it.each([
    {
      how: "",
      room: ROOM,
      // Its last step, 11 messages, fills a window of 500 pixels.
      endFrom: STEP_BELOW_ENDS,
      cameFrom: [
        "Answer 34",
        "Answer 24",
        "Answer 19",
        "Answer 9",
        "Question 0",
      ],
    },
    {
      how: ", with as many steps as fill the window",
      room: 2000,
      // Its last step doesn't fill a window of 2,000, with the one before it it does:
      // that one was drawn, out of sight, and is shown again.
      endFrom: 49,
      cameFrom: ["Answer 24", "Answer 19", "Answer 9", "Question 0"],
    },
  ])(
    "goes to the newest message as if it had been opened at its end: its last messages at once, and the rest above, a step at a time, none drawn twice$how",
    async ({ room, endFrom, cameFrom }) => {
      const { resized } = layOut({ room });
      const messages = exchanges(40);
      const api = fakeApi().on(TURNS, heldAfter("answer.sse", 5));
      const { user } = await openAndGoToThePlace(api, messages, {
        afterwards: [stored(messages)],
      });
      await leave(user);
      // An answer of the first step above: that step can't land until it is drawn,
      // and the steps after it, below and above, wait for it. Most of the
      // conversation is held back below what is shown.
      const arrive = holdDrawing("Answer 10");
      window.history.back();
      await waitFor(() => {
        expect(onThePage()).toEqual(
          textsOf(messages.slice(FIRST, STEP_BELOW_ENDS)),
        );
      });
      await afterAMoment();
      expect(onThePage()).toEqual(
        textsOf(messages.slice(FIRST, STEP_BELOW_ENDS)),
      );
      const changes = watch(() =>
        shownMessages().length === 0
          ? null
          : {
              shown: shownMessages().map(({ textContent }) => textContent),
              below: belowTheReader(),
            },
      );
      const shownFrom = watchShowing();

      await user.click(
        screen.getByRole("button", { name: "Go to the newest message" }),
      );

      // At the first change, its last messages and no more than fill the window, with
      // the reader at the end.
      expect(changes[0]).toEqual({
        shown: textsOf(messages.slice(endFrom)),
        below: 0,
      });
      expect(
        screen.queryByRole("button", { name: "Go to the newest message" }),
      ).not.toBeInTheDocument();

      // The rest comes in above, the nearest first, while the reader begins their next
      // question. (A drawing of the stand-in's that waits, and that a render of the page
      // came in between, is tried again only at the page's next render once it may go
      // on: the typing.)
      arrive();
      await user.type(screen.getByLabelText("Message"), "And");
      await waitFor(() => {
        expect(onThePage()).toEqual(textsOf(messages));
      });
      expect(shownFrom).toEqual(cameFrom);
      // None drawn twice, those shown around the place included.
      expect(drawnAnswers()).toHaveLength(40);
      expect(new Set(drawnAnswers())).toEqual(new Set(answers(0, 40)));
      // And the end is followed from then on.
      resized();
      expect(belowTheReader()).toBe(0);
      await user.click(screen.getByRole("button", { name: "Send" }));
      expect(await screen.findByText(/^Echo: Why/)).toBeInTheDocument();
      resized();
      expect(belowTheReader()).toBe(0);

      // Another conversation ends the reading while the page is still there.
      await leave(user);
    },
  );

  it("takes a question right after it opens there, after its last message, with the end shown as if it had opened there, and still brings the rest above", async () => {
    const { resized } = layOut();
    const messages = exchanges(40);
    const api = fakeApi().on(TURNS, () => streamed("answer.sse"));
    const { ask, user, SUSPENDED } = await openAndGoToThePlace(api, messages, {
      suspense: true,
      afterwards: [stored(messages)],
    });
    await leave(user);
    const arrive = holdDrawing("Answer 10");
    window.history.back();
    await waitFor(() => {
      expect(onThePage()).toEqual(
        textsOf(messages.slice(FIRST, STEP_BELOW_ENDS)),
      );
    });
    await afterAMoment();
    // Held back on both sides, and the page as it was.
    expect(onThePage()).toEqual(
      textsOf(messages.slice(FIRST, STEP_BELOW_ENDS)),
    );
    expect(screen.queryByText(SUSPENDED)).not.toBeInTheDocument();
    expect(log()).toHaveAttribute("aria-live", "off");
    const before = shownIds();
    const runs = watch(shownIds);

    await ask(user, ASKED);

    expect(await screen.findByText(ECHO)).toBeInTheDocument();
    await screen.findByRole("button", { name: "Send" });
    // At the first change the question makes, the conversation's last step, 11 messages
    // that fill the window, and none of those held back: the end, as if it had opened
    // there.
    const storedIds = messages.map(({ id }) => id);
    expect(
      runs
        .find((ids) => ids.join() !== before?.join())
        ?.filter((id) => storedIds.includes(id)),
    ).toEqual(storedIds.slice(STEP_BELOW_ENDS));
    // It follows the conversation's last message, with a run of the conversation in
    // order above it, at every change, and the page is at the end once it is laid out.
    expect(posted(api).parent_id).toBe("a39");
    const inOrder = [
      ...storedIds,
      posted(api).message?.id ?? "",
      RECORDED.answer,
    ];
    expect(runs.filter((ids) => !isARunOf(ids, inOrder))).toEqual([]);
    expect(onThePage().slice(-3)).toEqual(["Answer 39", ASKED, ECHO]);
    const conversationThen = [...textsOf(messages), ASKED, ECHO];
    resized();
    expect(belowTheReader()).toBe(0);
    expect(log()).toHaveAttribute("aria-live", "off");
    expect(screen.queryByText(SUSPENDED)).not.toBeInTheDocument();

    arrive();
    // A render of the page, at which React tries again the drawing that waited.
    await user.type(screen.getByLabelText("Message"), "And");

    await waitFor(() => {
      expect(onThePage()).toEqual(conversationThen);
    });
    expect(log()).not.toHaveAttribute("aria-live");
    expect(screen.queryByText(SUSPENDED)).not.toBeInTheDocument();
  });

  it("is shown whole when it is read again right after it opens there", async () => {
    layOut();
    const messages = exchanges(40);
    const api = fakeApi().on(TURNS, () => streamed("model_unavailable.sse"));
    const { ask, user, SUSPENDED } = await openAndGoToThePlace(api, messages, {
      suspense: true,
      afterwards: [
        stored(messages),
        // The model failed, so the page reads the conversation again: it holds the
        // question now, and the turn that failed to answer it.
        () =>
          json(
            200,
            conversation(
              RECORDED.conversation,
              [
                ...messages,
                question(posted(api).message?.id ?? "", ASKED, "a39"),
              ],
              turn({ status: "failed", error: "model_missing" }),
            ),
          ),
      ],
    });
    await leave(user);
    const arrive = holdDrawing("Answer 10");
    window.history.back();
    await waitFor(() => {
      expect(onThePage()).toEqual(
        textsOf(messages.slice(FIRST, STEP_BELOW_ENDS)),
      );
    });

    await ask(user, ASKED);

    // As the server has it: its end, with the question after its last message, and a
    // run of it in order above.
    expect(
      await screen.findByText("The model isn't on its server."),
    ).toBeInTheDocument();
    const conversationThen = [...textsOf(messages), ASKED];
    expect(isARunOf(onThePage(), conversationThen)).toBe(true);
    expect(onThePage().slice(-2)).toEqual(["Answer 39", ASKED]);
    expect(api.to(CONVERSATION)).toHaveLength(3);
    expect(screen.queryByText(SUSPENDED)).not.toBeInTheDocument();

    arrive();
    // A render of the page, at which React tries again the drawing that waited.
    await user.type(screen.getByLabelText("Message"), "And");

    await waitFor(() => {
      expect(onThePage()).toEqual(conversationThen);
    });
    expect(log()).not.toHaveAttribute("aria-live");
    expect(screen.queryByText(SUSPENDED)).not.toBeInTheDocument();
  });

  it("is left at its reader's place while the rest still comes in, and opens there again", async () => {
    layOut();
    const messages = exchanges(40);
    const { user } = await openAndGoToThePlace(fakeApi(), messages, {
      afterwards: [stored(messages)],
    });
    await leave(user);
    const arrive = holdDrawing("Answer 10");
    window.history.back();
    await waitFor(() => {
      expect(onThePage()).toEqual(
        textsOf(messages.slice(FIRST, STEP_BELOW_ENDS)),
      );
    });
    expect(offsetOf(PLACE.message)).toBe(PLACE.offset);
    // The reader goes on to "Answer 22", the 46th message, while steps above and below
    // are still held back, and leaves.
    scrollTo((45 - FIRST) * TALL + 10);
    expect(offsetOf("a22")).toBe(10);

    await leave(user);
    const seen = watch(() => offsetOf("a22"));
    window.history.back();

    // Around that place now: from the 24th message, with a step above and the two
    // below. The next step above holds "Answer 10".
    await waitFor(() => {
      expect(onThePage()).toEqual(textsOf(messages.slice(23)));
    });
    arrive();
    await waitFor(() => {
      expect(onThePage()).toEqual(textsOf(messages));
    });
    expect(seen[0]).toBe(10);
    expect(seen.filter((offset) => offset !== 10)).toEqual([]);
    expect(offsetOf("a22")).toBe(10);
  });

  it("has a screen reader say nothing of the messages that come below the place", async () => {
    layOut();
    const messages = exchanges(40);
    // Near the top: the messages drawn at once are its first 10, and the rest come in
    // below them alone.
    const { user } = await openStored(fakeApi(), messages, {
      afterwards: [stored(messages)],
    });
    await waitFor(() => {
      expect(onThePage()).toEqual(textsOf(messages));
    });
    scrollTo(2 * TALL + 30); // "Question 1", with 30 px
    await leave(user);
    const said = watch(() =>
      shownMessages().length === 0
        ? null
        : {
            messages: shownMessages().length,
            live: log().getAttribute("aria-live"),
          },
    );

    window.history.back();

    await waitFor(() => {
      expect(onThePage()).toEqual(textsOf(messages));
    });
    expect([...new Set(drawnAnswers())]).toEqual(answers(0, 40));
    expect(said[0]).toEqual({ messages: 10, live: "off" });
    expect(
      said.filter(({ messages, live }) => messages < 80 && live !== "off"),
    ).toEqual([]);
    expect(log()).not.toHaveAttribute("aria-live");
    expect(offsetOf("q1")).toBe(30);
  });

  it.each([
    {
      how: "among its last 8 messages: those above come in steps, and its end is drawn at once",
      count: 40,
      // "Answer 36", the 74th of 80.
      place: { message: "a36", index: 73 },
      first: 10,
      drawn: [
        ...answers(35, 40),
        ...answers(25, 35),
        ...answers(15, 25),
        ...answers(5, 15),
        ...answers(0, 5),
      ],
    },
    {
      how: "in a conversation of fewer than 10 messages: all of them at once",
      count: 4,
      // "Question 1", the 3rd of 8.
      place: { message: "q1", index: 2 },
      first: 8,
      drawn: answers(0, 4),
    },
  ])("opens at a place $how", async ({ count, place, first, drawn }) => {
    layOut();
    const messages = exchanges(count);
    const { user } = await openStored(fakeApi(), messages, {
      afterwards: [stored(messages)],
    });
    await waitFor(() => {
      expect(onThePage()).toEqual(textsOf(messages));
    });
    scrollTo(place.index * TALL + 30);
    expect(offsetOf(place.message)).toBe(30);
    await leave(user);
    const seen = watch(() => {
      const offset = offsetOf(place.message);
      return offset === null
        ? null
        : {
            offset,
            messages: shownMessages().length,
            last: shownMessages().at(-1)?.dataset.messageId,
          };
    });

    window.history.back();

    await waitFor(() => {
      expect(onThePage()).toEqual(textsOf(messages));
    });
    expect([...new Set(drawnAnswers())]).toEqual(drawn);
    // At the place, with the conversation's last message, from the moment it is on the
    // page.
    expect(seen[0]).toEqual({
      offset: 30,
      messages: first,
      last: `a${String(count - 1)}`,
    });
    expect(seen.filter(({ offset }) => offset !== 30)).toEqual([]);
    expect(log()).not.toHaveAttribute("aria-live");
  });

  it("opens at the block of its message that was at the top of the screen, though a block above it in that message is drawn smaller until it has been in view", async () => {
    const { painted } = layOut();
    // "Answer 20" is drawn in four blocks, the second of them smaller until it has been
    // near the window: as code was before a rule of the stylesheet, and as whatever is
    // drawn later may be. That one is "Code" here.
    const messages = exchanges(40).map((message) =>
      message.id === PLACE.message
        ? answer(
            "a20",
            "Answer 20\n\nCode of answer 20\n\nAnswer 20 goes on\n\nAnswer 20 ends",
            "q20",
          )
        : message,
    );
    const { user } = await openStored(fakeApi(), messages, {
      afterwards: [stored(messages)],
    });
    await waitFor(() => {
      expect(onThePage()).toEqual(textsOf(messages));
    });
    // The reader reads that block, which then has its real height, and goes on: the top
    // of the screen 20 pixels into the paragraph after it.
    scrollTo(PLACED * TALL + PARAGRAPH);
    painted();
    scrollTo(PLACED * TALL + PARAGRAPH + CODE_SEEN + 20);
    expect(blockOffsetOf("Answer 20 goes on")).toBe(20);
    await leave(user);
    const seen = watch(() => blockOffsetOf("Answer 20 goes on"));

    window.history.back();

    await waitFor(() => {
      expect(onThePage()).toEqual(textsOf(messages));
    });
    // Drawn anew, that block is smaller again, above the screen. The paragraph at the
    // top of the screen stands where it stood, from the moment it is on the page.
    expect(
      blockNamed("Code of answer 20")?.getBoundingClientRect().height,
    ).toBe(CODE_UNSEEN);
    expect(seen[0]).toBe(20);
    expect(seen.filter((offset) => offset !== 20)).toEqual([]);
  });

  it.each([
    {
      how: "taller than the one it was left in, the 10 drawn at once filling it: as near the place as there is room for, and no more drawn at once",
      room: 900,
      // Its last 10 are 1,000 pixels: the place, 330 down in them, can't come to the
      // window's top, and what is above it changes nothing of that.
      first: 10,
    },
    {
      how: "the 10 drawn at once don't fill: the steps above are shown before anything is, as many as fill it, and no more",
      room: 2500,
      // 10 and 20 are 3,000 pixels.
      first: 30,
    },
  ])(
    "opens at a place near its end, with nothing held back below, in a window $how",
    async ({ room, first }) => {
      const { windowOf } = layOut();
      const messages = exchanges(40);
      const { user } = await openStored(fakeApi(), messages, {
        afterwards: [stored(messages)],
      });
      await waitFor(() => {
        expect(onThePage()).toEqual(textsOf(messages));
      });
      // "Answer 36", the 74th of 80, 30 pixels in: 170 pixels above the end.
      scrollTo(73 * TALL + 30);
      await leave(user);
      // A window taller on the way back, as when a draft of several lines left the box,
      // or the window was made larger.
      windowOf(room);
      const seen = watch(() =>
        shownMessages().length === 0 ? null : shownMessages().length,
      );

      window.history.back();

      await waitFor(() => {
        expect(onThePage()).toEqual(textsOf(messages));
      });
      expect(seen[0]).toBe(first);
    },
  );

  it.each([
    { how: "", strict: false },
    // React undoes and runs again the effects that put the reader at the place while
    // more is shown below for it. Where the reader is in that moment is no place of
    // theirs, and nothing is noted of it.
    { how: " under React's StrictMode", strict: true },
  ])(
    "opens at the place in a window taller than the messages drawn at once below it$how",
    async ({ strict }) => {
      // Every place the page notes for this conversation, from the module that keeps
      // them.
      const kept: (Places.Spot | null)[] = [];
      vi.doMock("@/lib/chat/places", async (importOriginal) => {
        const places = await importOriginal<typeof Places>();
        return {
          ...places,
          keepPlace: (...noted: Parameters<typeof places.keepPlace>) => {
            if (noted[1] === RECORDED.conversation) {
              kept.push(noted[2]);
            }
            places.keepPlace(...noted);
          },
        };
      });
      // The 15 messages from the place on are 1,500 pixels high, in a window of 2,000:
      // more of them are drawn at once, before the page is painted.
      layOut({ room: 2000 });
      const messages = exchanges(40);
      const { user } = await openAndGoToThePlace(fakeApi(), messages, {
        strict,
        afterwards: [stored(messages)],
      });
      await leave(user);
      kept.length = 0;
      const seen = watch(() => offsetOf(PLACE.message));

      window.history.back();

      await waitFor(() => {
        expect(onThePage()).toEqual(textsOf(messages));
      });
      expect(seen[0]).toBe(PLACE.offset);
      expect(seen.filter((offset) => offset !== PLACE.offset)).toEqual([]);
      expect([...new Set(drawnAnswers())]).toEqual(AROUND_THE_PLACE);
      // Noted since: the reader's place, when they leave, and nothing before.
      await leave(user);
      expect(kept).toEqual([
        { message: PLACE.message, path: [], offset: PLACE.offset },
      ]);
    },
  );

  it("keeps its reader at the place while an answer is written at its end, draws nothing at once for it, and has the answer after its last message once the steps below are in", async () => {
    layOut();
    const messages = exchanges(40);
    const writing = sentByHand("answer.sse");
    const api = fakeApi().on(STREAM, writing.answer);
    const { user } = await openAndGoToThePlace(api, messages, {
      afterwards: [
        // Another window asked a question meanwhile, whose answer is being written.
        () =>
          json(
            200,
            conversation(
              RECORDED.conversation,
              [...messages, question("q40", "Question 40", "a39")],
              turn({ status: "running", finish_reason: null, credits: null }),
            ),
          ),
      ],
    });
    await leave(user);
    // The first step below, which holds "Answer 30", can't land until it is drawn, and
    // every step after it waits for it: two steps below are still to come when the
    // answer arrives.
    const arrive = holdDrawing("Answer 30");
    const inOrder = [...messages.map(({ id }) => id), "q40", RECORDED.answer];
    const seen = watch(() => {
      const ids = shownIds();
      const offset = offsetOf(PLACE.message);
      return ids === null || offset === null ? null : { ids, offset };
    });
    // Its first words are there as soon as the page reads along.
    writing.send(5);

    window.history.back();

    // The page reads along, and the answer waits for the steps to reach the end.
    expect(
      await screen.findByRole("button", { name: "Stop" }),
    ).toBeInTheDocument();
    await afterAMoment();
    expect(screen.queryByText(/^Echo: /)).not.toBeInTheDocument();
    expect(offsetOf(PLACE.message)).toBe(PLACE.offset);
    // The control: the 10 drawn at once alone, and the two steps below still to come.
    expect(shownIds()).toEqual(
      messages.slice(FIRST, FIRST + 10).map(({ id }) => id),
    );

    arrive();
    // More of the answer: a render of the page, at which React tries again the drawing
    // that waited.
    writing.send(2);
    await waitFor(() => {
      expect(shownMessages().map(({ dataset }) => dataset.messageId)).toEqual(
        inOrder,
      );
    });
    writing.end();
    expect(await screen.findByText(ECHO)).toBeInTheDocument();
    // At every change, what is shown is one run of the conversation's messages, in
    // order, the reader at their place, and no more than a step more than before.
    expect(seen.filter(({ ids }) => !isARunOf(ids, inOrder))).toEqual([]);
    expect(seen.filter(({ offset }) => offset !== PLACE.offset)).toEqual([]);
    expect(
      seen.filter(
        ({ ids }, at) => ids.length - (seen[at - 1]?.ids.length ?? 0) > 20,
      ),
    ).toEqual([]);
  });

  it("draws what a later look at it brings while the steps below are short of its end, after its last message, once they reach it, and nothing at once for it", async () => {
    layOut();
    const messages = exchanges(40);
    const asked = question("q40", "Question 40", "a39");
    const running = conversation(
      RECORDED.conversation,
      [...messages, asked],
      turn({ status: "running", finish_reason: null, credits: null }),
    );
    // The turn's stream can't be read along with: the page looks at the conversation
    // every second and a half until the answer is written.
    const api = fakeApi().on(STREAM, () => nothing());
    const { user } = await openAndGoToThePlace(api, messages, {
      afterwards: [
        () => json(200, running),
        () => json(200, running),
        () =>
          json(
            200,
            conversation(RECORDED.conversation, [
              ...messages,
              asked,
              answer("a40", "Answer 40", "q40"),
            ]),
          ),
      ],
    });
    await leave(user);
    // The first step below waits, and every step after it.
    const arrive = holdDrawing("Answer 30");
    const inOrder = [...messages.map(({ id }) => id), "q40", "a40"];
    const seen = watch(() => {
      const ids = shownIds();
      const offset = offsetOf(PLACE.message);
      return ids === null || offset === null ? null : { ids, offset };
    });

    window.history.back();

    // A look found the answer written: read when the page came back, when the stream
    // couldn't be read, and once more a second and a half later.
    await waitFor(
      () => {
        expect(api.to(CONVERSATION)).toHaveLength(4);
      },
      { timeout: 5000 },
    );
    await afterAMoment();
    expect(screen.queryByText("Answer 40")).not.toBeInTheDocument();
    expect(shownIds()).toEqual(
      messages.slice(FIRST, FIRST + 10).map(({ id }) => id),
    );

    arrive();
    // A render of the page, at which React tries again the drawing that waited.
    await user.type(screen.getByLabelText("Message"), "And");

    await waitFor(() => {
      expect(shownIds()).toEqual(inOrder);
    });
    expect(seen.filter(({ ids }) => !isARunOf(ids, inOrder))).toEqual([]);
    expect(seen.filter(({ offset }) => offset !== PLACE.offset)).toEqual([]);
    expect(
      seen.filter(
        ({ ids }, at) => ids.length - (seen[at - 1]?.ids.length ?? 0) > 20,
      ),
    ).toEqual([]);
    // The page's look waits a second and a half: more than the five seconds a test
    // has may go by on a busy machine.
  }, 15_000);

  it("shows its end, and nothing less, when the newest message is asked for before the step decided next is", async () => {
    layOut();
    // 60 exchanges, so that steps below are still to come after the next one: the
    // place's 10, then steps of 20 below to 120, and above.
    const messages = exchanges(60);
    const inOrder = messages.map(({ id }) => id);
    const { user } = await openAndGoToThePlace(fakeApi(), messages, {
      afterwards: [stored(messages)],
    });
    await leave(user);
    // From the press on: what is shown at every change, and how far the reader is from
    // the end.
    const seen: { ids: string[]; below: number }[] = [];
    const watching = new MutationObserver(() => {
      if (document.querySelector('[role="log"]') !== null) {
        seen.push({ ids: shownIds() ?? [], below: belowTheReader() });
      }
    });
    // The press comes at the commit of the first step drawn above, in a transition:
    // what an observer of the page is told comes right after a commit's changes, and
    // before that commit's effects, of which one decides the next step below. So the
    // press comes before that decision is made, and the decision's update after the
    // button's.
    let pressed = false;
    const pressing = new MutationObserver((records) => {
      const placed = document
        .querySelector(`[data-message-id="${PLACE.message}"]`)
        ?.closest('[role="log"] > *');
      const above = records
        .flatMap(({ addedNodes }) => [...addedNodes])
        .some(
          (added) =>
            added instanceof Element &&
            added.parentElement?.getAttribute("role") === "log" &&
            placed !== null &&
            placed !== undefined &&
            (added.compareDocumentPosition(placed) &
              Node.DOCUMENT_POSITION_FOLLOWING) !==
              0,
        );
      // The button is on the page all along: it can be pressed while it is displayed.
      const button = document.querySelector<HTMLElement>(
        'button[aria-label="Go to the newest message"]',
      );
      if (above && button !== null && !button.hidden) {
        pressing.disconnect();
        pressed = true;
        watching.observe(document.body, {
          subtree: true,
          childList: true,
          attributes: true,
          attributeFilter: ["hidden"],
        });
        // As the browser sends it, and not inside `act`, which would work through every
        // render and effect it brings before handing back: what the page shows between
        // them is what this is about.
        button.click();
      }
    });
    pressing.observe(document.body, { subtree: true, childList: true });
    onTestFinished(() => {
      pressing.disconnect();
      watching.disconnect();
    });

    window.history.back();

    await waitFor(() => {
      expect(shownIds()).toEqual(inOrder);
    });
    // The control: the press was made at that moment.
    expect(pressed).toBe(true);
    expect(seen.length).toBeGreaterThan(0);
    // One run that ends with the conversation's last message, never nothing, and the
    // reader at the end, at every change from the press on.
    expect(
      seen.filter(
        ({ ids }) =>
          ids.length === 0 ||
          !isARunOf(ids, inOrder) ||
          ids.at(-1) !== inOrder.at(-1),
      ),
    ).toEqual([]);
    expect(seen.filter(({ below }) => below !== 0)).toEqual([]);
  });

  it("opens at its end once its reader went from their place to the newest message", async () => {
    layOut();
    const messages = exchanges(40);
    const { user } = await openAndGoToThePlace(fakeApi(), messages, {
      afterwards: [stored(messages)],
    });
    await leave(user);
    window.history.back();
    await waitFor(() => {
      expect(offsetOf(PLACE.message)).toBe(PLACE.offset);
    });
    await user.click(
      screen.getByRole("button", { name: "Go to the newest message" }),
    );
    await leave(user);
    const below = watch(() =>
      shownMessages().length === 0 ? null : belowTheReader(),
    );

    window.history.back();

    await waitFor(() => {
      expect(onThePage()).toEqual(textsOf(messages));
    });
    // As without a place: at its end from the first change, its newest first.
    expect(below[0]).toBe(0);
    expect([...new Set(drawnAnswers())]).toEqual([
      ...answers(35, 40),
      ...answers(25, 35),
      ...answers(15, 25),
      ...answers(5, 15),
      ...answers(0, 5),
    ]);
  });

  it("lets the places kept for the tab go as soon as the session changes hands, before a conversation asks for one", async () => {
    layOut();
    const messages = exchanges(40);
    const api = fakeApi().on(SIGN_UP, () =>
      json(201, account({ id: GUEST.id })),
    );
    const { user } = await openAndGoToThePlace(api, messages, {
      afterwards: [stored(messages)],
    });
    await leave(user);
    // The page was left once, and the tab has the place.
    window.dispatchEvent(new Event("pagehide"));
    expect(window.sessionStorage.getItem("actio.places")).not.toBeNull();

    // Signed up from a new chat, which goes back to a new chat: no conversation asks
    // for its place.
    await user.click(
      within(screen.getByRole("banner")).getByRole("link", { name: "Sign up" }),
    );
    await user.type(screen.getByLabelText("Email address"), "ada@example.com");
    await user.type(
      screen.getByLabelText("Password", { exact: true }),
      "correct horse battery",
    );
    await user.click(screen.getByRole("button", { name: "Sign up" }));

    await screen.findByRole("link", { name: "Account: ada@example.com" });
    await screen.findByText("What would you like to know?");
    expect(window.sessionStorage.getItem("actio.places")).toBeNull();
  });
});

/** The block of a message shown that says `text`, as the stand-in draws a text of
 * several paragraphs. */
function blockNamed(text: string): Element | undefined {
  return shownMessages()
    .flatMap(blocksOf)
    .find((block) => block.textContent === text);
}

/** How far below the top of the block that says `text` the top of the screen is, in
 * pixels: nothing while it isn't shown. */
function blockOffsetOf(text: string): number | null {
  const block = blockNamed(text);
  return block === undefined
    ? null
    : scroller().getBoundingClientRect().top -
        block.getBoundingClientRect().top;
}

/** The ids of the messages shown, from the top: nothing while none is. */
function shownIds(): string[] | null {
  const ids = shownMessages().map(({ dataset }) => dataset.messageId ?? "");
  return ids.length === 0 ? null : ids;
}

/** Whether `shown` is one run of `whole`, in its order, with nothing missing inside. */
function isARunOf(shown: string[], whole: string[]): boolean {
  const from = whole.indexOf(shown[0] ?? "");
  return (
    from !== -1 && shown.every((one, index) => whole[from + index] === one)
  );
}
