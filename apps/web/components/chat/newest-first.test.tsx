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
// does.
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import type { UserEvent } from "@testing-library/user-event";
import { use, useLayoutEffect } from "react";
import { beforeEach, describe, expect, it, onTestFinished, vi } from "vitest";

import type { StoredMessage } from "@/lib/api/client";
import { clockAt } from "@/tests/clock";
import {
  answer,
  conversation,
  fakeApi,
  heldAfter,
  json,
  later,
  problem,
  question,
  streamed,
  turn,
} from "@/tests/fake-api";
import type { Call, FakeApi } from "@/tests/fake-api";
import { RECORDED } from "@/tests/recorded";

import { STILL_AFTER_MS } from "./follow";

const CONVERSATION = "GET /api/conversations/*";
const TURNS = "POST /api/conversations/*/turns";

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
  if (drawing.waits?.text === text) {
    use(drawing.waits.until);
  }
  useLayoutEffect(() => {
    drawing.drawn.push({ text, writing });
  });
  return <p className={className}>{text}</p>;
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
      // question now, and the turn that failed to answer it.
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
              turn({ status: "failed", error: "model_unavailable" }),
            ),
          ),
      ],
    });
    await waitFor(() => {
      expect(onThePage()).toEqual(newest);
    });

    await ask(user, ASKED);

    // As the server has it, still from the same message on: the answer the page held
    // for the failed turn, which had nothing in it, is gone.
    await waitFor(() => {
      expect(onThePage()).toEqual([...newest, ASKED]);
    });
    expect(api.to(CONVERSATION)).toHaveLength(2);
    expect(
      await screen.findByText(/can't be reached, or is too busy/),
    ).toBeInTheDocument();
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
