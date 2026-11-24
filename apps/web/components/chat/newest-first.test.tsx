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

beforeEach(() => {
  drawing.fetched = 0;
  drawing.drawn = [];
  drawing.waits = null;
  // Every module is loaded anew by the next import, as on a page that was just opened.
  vi.resetModules();
  vi.doMock("./markdown", () => {
    drawing.fetched += 1;
    return { default: StandIn };
  });
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
      // 20 messages, then the 20 before them, then the first 10.
      expect([...new Set(drawnTexts())]).toEqual([
        ...answers(15, 25),
        ...answers(5, 15),
        ...answers(0, 5),
      ]);
    },
  );

  it.each([
    { count: 25, said: ["off", null] },
    { count: 11, said: ["off", null] }, // two messages held back
    { count: 10, said: [null] }, // twenty messages: whole at once
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
      const newest = textsOf(messages.slice(30));
      // Nothing older is drawn before the reader has scrolled.
      const arrive = holdDrawing("Answer 10");
      await openStored(fakeApi(), messages);
      await waitFor(() => {
        expect(onThePage()).toEqual(newest);
      });

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
      // the first 10.
      expect(shownFrom).toEqual(["Question 5", "Question 0"]);
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
      // The newest 20 stay, and the question and its answer come after them.
      left: 20,
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
      left: 19,
      came: 1,
    },
  ])(
    "draws $how, while its older messages aren't all shown, as arriving",
    async ({ last, asks, left, came }) => {
      // The reader scrolls, and the clock stands still: nothing older is shown.
      clockAt();
      const messages = [...exchanges(25).slice(0, -1), last];
      const newest = textsOf(messages.slice(30));
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
    const newest = textsOf(messages.slice(30));
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
    const newest = textsOf(messages.slice(30));
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
      expect(onThePage()).toEqual(textsOf(messages.slice(30)));
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
 * with "Code" is code. The element that scrolls has `room` for them. Nothing else has
 * a size. The page is told that a size changed only when the test calls `resized`, as a
 * browser tells it once it has laid the page out; and code has been near the window
 * when the test calls `painted`, as a browser draws a frame. */
function layOut({ room = ROOM }: { room?: number } = {}): {
  resized: () => void;
  painted: () => void;
} {
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
 * messages drawn at once when it opens there again begin with its 37th: 5 before the
 * place, and 15 from it on. With the step below them, they end before its 77th. */
const PLACE = { message: "a20", offset: 30 };
const PLACED = 41;
const FIRST = 36;
const STEP_BELOW_ENDS = 76;

/** What each step draws, when a conversation of 40 exchanges opens at `PLACE`: the
 * answers drawn at once, then those of the step below them, of the step above them, of
 * the next step below, and of the next above. */
const AROUND_THE_PLACE = [
  ...answers(18, 28),
  ...answers(28, 38),
  ...answers(8, 18),
  ...answers(38, 40),
  ...answers(0, 8),
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
      // From the moment it is on the page, with the 20 around the place, the place
      // stands where it stood, and a screen reader is told nothing of what comes.
      expect(seen[0]).toEqual({
        offset: PLACE.offset,
        messages: 20,
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
      ...answers(15, 25),
      ...answers(5, 15),
      ...answers(0, 5),
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
    // As without a place: its newest 20 first, at its end, then the older ones.
    expect([...new Set(drawnAnswers())]).toEqual([
      ...answers(30, 40),
      ...answers(20, 30),
      ...answers(10, 20),
      ...answers(0, 10),
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
        ...answers(30, 40),
        ...answers(20, 30),
        ...answers(10, 20),
        ...answers(0, 10),
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

  it("goes to the newest message with every message on the way there, and follows the end from then on", async () => {
    const { resized } = layOut();
    const messages = exchanges(40);
    const api = fakeApi().on(TURNS, heldAfter("answer.sse", 5));
    const { user } = await openAndGoToThePlace(api, messages, {
      afterwards: [stored(messages)],
    });
    await leave(user);
    // An answer of the first step above: that step can't land until it is drawn, and
    // the steps after it, below and above, wait for it.
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

    await user.click(
      screen.getByRole("button", { name: "Go to the newest message" }),
    );

    // Every message from the place on is there, in order, and the page is at the end.
    expect(onThePage()).toEqual(textsOf(messages.slice(FIRST)));
    expect(belowTheReader()).toBe(0);
    expect(
      screen.queryByRole("button", { name: "Go to the newest message" }),
    ).not.toBeInTheDocument();

    // From then on the end is followed. While the reader begins their next question,
    // the older messages come in above, and the page is laid out anew. (A drawing of
    // the stand-in's that waits, and that a render of the page came in between, is
    // tried again only at the page's next render once it may go on: the typing.)
    arrive();
    await user.type(screen.getByLabelText("Message"), "And");
    await waitFor(() => {
      expect(onThePage()).toEqual(textsOf(messages));
    });
    resized();
    expect(belowTheReader()).toBe(0);
    // And its answer is followed as it arrives.
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText(/^Echo: Why/)).toBeInTheDocument();
    resized();
    expect(belowTheReader()).toBe(0);

    // Another conversation ends the reading while the page is still there.
    await leave(user);
  });

  it("takes a question right after it opens there, after its last message, and still brings the rest above", async () => {
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

    await ask(user, ASKED);

    expect(await screen.findByText(ECHO)).toBeInTheDocument();
    await screen.findByRole("button", { name: "Send" });
    // It follows the conversation's last message, with all of them above it in order,
    // and the page is at the end once it is laid out.
    expect(posted(api).parent_id).toBe("a39");
    expect(onThePage()).toEqual([
      ...textsOf(messages.slice(FIRST)),
      ASKED,
      ECHO,
    ]);
    resized();
    expect(belowTheReader()).toBe(0);
    expect(log()).toHaveAttribute("aria-live", "off");
    expect(screen.queryByText(SUSPENDED)).not.toBeInTheDocument();

    arrive();

    await waitFor(() => {
      expect(onThePage()).toEqual([...textsOf(messages), ASKED, ECHO]);
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

    // As the server has it, from the messages around the place on.
    expect(
      await screen.findByText("The model isn't on its server."),
    ).toBeInTheDocument();
    expect(onThePage()).toEqual([...textsOf(messages.slice(FIRST)), ASKED]);
    expect(api.to(CONVERSATION)).toHaveLength(3);
    expect(screen.queryByText(SUSPENDED)).not.toBeInTheDocument();

    arrive();

    await waitFor(() => {
      expect(onThePage()).toEqual([...textsOf(messages), ASKED]);
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

    // Around that place now: from the 41st message, and the step below.
    await waitFor(() => {
      expect(onThePage()).toEqual(textsOf(messages.slice(40)));
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
    // Near the top: the messages drawn at once are its first 20, and the rest come in
    // below them alone.
    const { user } = await openStored(fakeApi(), messages, {
      afterwards: [stored(messages)],
    });
    await waitFor(() => {
      expect(onThePage()).toEqual(textsOf(messages));
    });
    scrollTo(5 * TALL + 30); // "Answer 2", with 30 px
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
    expect(said[0]).toEqual({ messages: 20, live: "off" });
    expect(
      said.filter(({ messages, live }) => messages < 80 && live !== "off"),
    ).toEqual([]);
    expect(log()).not.toHaveAttribute("aria-live");
    expect(offsetOf("a2")).toBe(30);
  });

  it.each([
    {
      how: "among its last 15 messages: those above come in steps, and its end is drawn at once",
      count: 40,
      // "Answer 36", the 74th of 80.
      place: { message: "a36", index: 73 },
      first: 20,
      drawn: [
        ...answers(30, 40),
        ...answers(20, 30),
        ...answers(10, 20),
        ...answers(0, 10),
      ],
    },
    {
      how: "in a conversation of fewer than 20 messages: all of them at once",
      count: 8,
      // "Answer 4", the 10th of 16.
      place: { message: "a4", index: 9 },
      first: 16,
      drawn: answers(0, 8),
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

  it("draws an answer it reads along with after its last message, with every message before it", async () => {
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
    // The steps above wait for an answer of the first of them, and so the steps below
    // after the first wait too.
    const arrive = holdDrawing("Answer 10");
    const inOrder = [...messages.map(({ id }) => id), "q40", RECORDED.answer];
    const shown = watch(() => {
      const ids = shownMessages().map(({ dataset }) => dataset.messageId ?? "");
      return ids.length === 0 ? null : ids;
    });
    // Its first words are there as soon as the page reads along.
    writing.send(5);

    window.history.back();

    expect(await screen.findByText(/^Echo: Why/)).toBeInTheDocument();
    arrive();
    writing.end();
    await waitFor(() => {
      expect(shownMessages().map(({ dataset }) => dataset.messageId)).toEqual(
        inOrder,
      );
    });
    // At every change, what is shown is one run of the conversation's messages, in
    // order: nothing missing before the answer, which follows its last message.
    expect(
      shown.filter((ids) => {
        const from = inOrder.indexOf(ids[0] ?? "");
        return (
          from === -1 ||
          ids.join() !== inOrder.slice(from, from + ids.length).join()
        );
      }),
    ).toEqual([]);
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
