// The code that shows a model's words as markdown is a piece apart from the page, which
// the page fetches ahead, once it knows whose session it has, the list of conversations
// has been read and a frame has shown it (message.tsx). An answer is never shown first
// as the marks it was written in, to be swapped for markdown a moment later (ADR 0016,
// revisions of 2026-11-21 and 2026-11-22). While the code is on its way, a stored
// conversation and an answer that arrives wait for it, three seconds at the most, by
// one wait for the page (revision "an answer that arrives waits for the renderer too").
//
// The page holds that code for as long as it is open. So each test here opens the app
// anew, from modules loaded anew: a page that has fetched nothing yet, and hasn't
// waited. The code comes through the factory below, which counts each time it is
// fetched, and gives it only when the test lets it arrive. A test that holds it back
// lets it arrive before it ends: still on its way, it would be handed to the next test.
import { act, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, onTestFinished, vi } from "vitest";

import { clockAt, SECOND } from "@/tests/clock";
import {
  answer,
  conversation,
  fakeApi,
  GUEST,
  json,
  later,
  listed,
  question,
  sentByHand,
  streamed,
  summary,
} from "@/tests/fake-api";
import type { FakeApi } from "@/tests/fake-api";
import { holdFrames } from "@/tests/frames";
import { RECORDED } from "@/tests/recorded";

import type * as MarkdownModule from "./markdown";

const code = {
  /** How many times the code was fetched. */
  fetched: 0,
  /** What its fetch waits for. */
  arrival: Promise.resolve(),
  /** Whether it can't be had, as after an upgrade under an open page. */
  gone: false,
};

beforeEach(() => {
  code.fetched = 0;
  code.arrival = Promise.resolve();
  code.gone = false;
  // Every module is loaded anew by the next import, the code's too: `resetModules`
  // leaves a mocked module as it was, and a mock given again starts over.
  vi.resetModules();
  vi.doMock("./markdown", async (importOriginal) => {
    code.fetched += 1;
    await code.arrival;
    if (code.gone) {
      throw new Error("Failed to load chunk");
    }
    return importOriginal<typeof MarkdownModule>();
  });
});

/** Keep the code from arriving until the test calls what this returns. */
function holdCode(): () => void {
  let arrive: () => void = () => undefined;
  code.arrival = new Promise<void>((resolve) => {
    arrive = resolve;
  });
  return arrive;
}

/** The app, as a page that has just been loaded: nothing of it fetched yet. */
async function newPage() {
  return import("@/tests/open");
}

const LIST = "GET /api/conversations";
const CONVERSATION = "GET /api/conversations/*";
const TURNS = "POST /api/conversations/*/turns";

/** A stored conversation, whose answer is told apart at once: as written, it holds
 * "##"; as markdown, it is a heading and a list. */
function stored(): Response {
  return json(
    200,
    conversation(RECORDED.conversation, [
      question(RECORDED.question, "Which parts?"),
      answer(
        RECORDED.answer,
        "## Parts\n\n1. The `saddle`\n2. The chain",
        RECORDED.question,
      ),
    ]),
  );
}

const ANSWER = 'article[aria-label="Actio"]';

/** What an answer says before its first word, and while its words wait for the code. */
const WAITING = "Waiting for the model…";
/** An answer that says that, and nothing else. */
const ONLY_WAITING = new RegExp(`^${WAITING}$`);

/** From now on, note the text of each answer every time the page changes: what was
 * put into an answer and what was taken out of it, and each answer that was put on
 * the page. A text that was shown and replaced at once is noted too, as the text
 * taken out. */
function watchAnswers(): string[] {
  const seen: string[] = [];
  function inAnswer(node: Node): boolean {
    const element = node instanceof Element ? node : node.parentElement;
    return (element?.closest(ANSWER) ?? null) !== null;
  }
  const observer = new MutationObserver((records) => {
    for (const record of records) {
      if (inAnswer(record.target)) {
        for (const node of [...record.addedNodes, ...record.removedNodes]) {
          seen.push(node.textContent ?? "");
        }
        if (record.type === "characterData") {
          seen.push(record.oldValue ?? "", record.target.textContent ?? "");
        }
        continue;
      }
      for (const node of record.addedNodes) {
        if (node instanceof Element) {
          for (const added of [node, ...node.querySelectorAll(ANSWER)]) {
            if (added.matches(ANSWER)) {
              seen.push(added.textContent);
            }
          }
        }
      }
    }
  });
  observer.observe(document.body, {
    childList: true,
    subtree: true,
    characterData: true,
    characterDataOldValue: true,
  });
  onTestFinished(() => {
    observer.disconnect();
  });
  return seen;
}

/** Give the page a moment with what has just arrived, when nothing is to change for
 * it: whatever it does with an answer takes it milliseconds. */
async function afterAMoment(): Promise<void> {
  await act(
    () =>
      new Promise<void>((resolve) => {
        setTimeout(resolve, 100);
      }),
  );
}

/** Wait until the page has fetched the code, once, and it has arrived. A test that
 * ended while it was still on its way would hand it to the next one, whose modules are
 * loaded anew: the factory above would not be asked again there. */
async function fetchedOnce(): Promise<void> {
  await waitFor(() => {
    expect(code.fetched).toBe(1);
  });
  await act(async () => {
    await vi.dynamicImportSettled();
  });
}

describe("the code that shows answers as markdown", () => {
  it("is not fetched before the page knows whose session it has", async () => {
    const session = later(() => json(201, GUEST));
    fakeApi().on("POST /api/session/guest", session.answer);
    const { open } = await newPage();

    open();

    expect(await screen.findByText("Starting…")).toBeInTheDocument();
    // Frames have been painted meanwhile, and whatever the page has begun to fetch
    // has arrived by now.
    await afterAMoment();
    await act(async () => {
      await vi.dynamicImportSettled();
    });
    expect(code.fetched).toBe(0);

    session.release();

    // Then it is, once the page has started: this page does ask for it.
    expect(await screen.findByLabelText("Message")).toBeInTheDocument();
    await fetchedOnce();
    // Ahead of any answer: there is none.
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
  });

  it("is not fetched while the list of conversations is on its way", async () => {
    // Its code, run as it arrives, would keep the page from painting the list's rows
    // (ADR 0016, revision of 2026-11-22).
    const reading = later(() => json(200, listed([])));
    fakeApi().on(LIST, reading.answer);
    const { open } = await newPage();

    open();

    // The page knows whose session it has, and frames are painted meanwhile.
    expect(await screen.findByLabelText("Message")).toBeInTheDocument();
    await afterAMoment();
    await act(async () => {
      await vi.dynamicImportSettled();
    });
    expect(code.fetched).toBe(0);

    reading.release();

    expect(await screen.findByText(/No conversations yet/)).toBeInTheDocument();
    await fetchedOnce();
  });

  it("is fetched once a frame has come after the list of conversations, and not before", async () => {
    // Not in the moment the list arrives: once the browser has painted it. When in
    // that frame it is asked for is held by lib/after-paint.test.ts: in the task
    // after the frame's callback, which runs before the frame is painted.
    const nextFrame = holdFrames();
    fakeApi();
    const { open } = await newPage();

    open();

    expect(await screen.findByText(/No conversations yet/)).toBeInTheDocument();
    // No frame comes, however long the page waits.
    await afterAMoment();
    await act(async () => {
      await vi.dynamicImportSettled();
    });
    expect(code.fetched).toBe(0);

    act(() => {
      nextFrame();
    });

    await fetchedOnce();
  });

  it("is fetched when the list of conversations can't be read", async () => {
    // The page is not left without it because of that.
    fakeApi().on(LIST, () => {
      throw new TypeError("fetch failed");
    });
    const { open } = await newPage();

    open();

    expect(
      await screen.findByText("The list couldn’t be read."),
    ).toBeInTheDocument();
    await fetchedOnce();
  });

  it("is fetched when someone begins to write a question while the list of conversations is still on its way", async () => {
    // The page hasn't asked for it ahead then, and may never: nothing keeps a question
    // from being written and asked while the list is read. Asked for only once the
    // answer is on the page, the answer's words would wait for it there.
    const reading = later(() => json(200, listed([])));
    fakeApi()
      .on(LIST, reading.answer)
      .on(TURNS, () => streamed("markdown.sse"));
    const { ask, open } = await newPage();
    const user = open();
    const box = await screen.findByLabelText("Message");
    await afterAMoment();
    expect(code.fetched).toBe(0);
    const seen = watchAnswers();

    // At the first letter: nothing is sent yet.
    await user.type(box, "N");
    await fetchedOnce();
    expect(screen.queryByRole("article")).not.toBeInTheDocument();

    await ask(user, "ame **two bike** parts.");

    // The recorded answer: "Echo: Name **two bike** parts.".
    expect(await screen.findByText("two bike")).toHaveClass("font-semibold");
    expect(seen.some((text) => text.includes("Echo: Name"))).toBe(true);
    expect(seen.filter((text) => text.includes("*"))).toEqual([]);

    // The list comes after all: the page doesn't ask a second time.
    reading.release();
    expect(await screen.findByText(/No conversations yet/)).toBeInTheDocument();
    await afterAMoment();
    expect(code.fetched).toBe(1);
  });

  it("is fetched for an answer that is on the page before anything else asked for it", async () => {
    // The page asks once the list of conversations is on the page, before any answer.
    // Should an answer ever be shown without that, it asks itself, and is not left as
    // written for good. At once: no frame comes here.
    holdFrames();
    const { Message } = await import("./message");

    render(
      <Message
        message={{
          id: RECORDED.answer,
          role: "assistant",
          parts: [{ type: "text", text: "## Parts" }],
        }}
      />,
    );

    expect(
      await screen.findByRole("heading", { level: 2, name: "Parts" }),
    ).toBeVisible();
    expect(code.fetched).toBe(1);
  });

  it("is fetched for an answer that is on the page before its first word", async () => {
    // Asked for only once its words are there, they would wait for the code from then
    // on, where it could be on its way already.
    holdFrames();
    const { Message } = await import("./message");

    render(
      <Message
        message={{ id: RECORDED.answer, role: "assistant", parts: [] }}
        writing
      />,
    );

    expect(screen.getByText(WAITING)).toBeInTheDocument();
    await fetchedOnce();
  });

  // This one's answer arrives in pieces, and its marks in different pieces. A render
  // as written between two of them could go unseen here: the stored conversation
  // below, whose whole text is there at its first render, is the test that can't
  // miss one.
  it("shows the first answer as markdown from its first render, once it is here", async () => {
    fakeApi().on(TURNS, () => streamed("markdown.sse"));
    const { ask, open } = await newPage();
    const user = open();
    await screen.findByLabelText("Message");
    await waitFor(() => {
      expect(code.fetched).toBe(1);
    });
    await act(async () => {
      await vi.dynamicImportSettled();
    });
    const seen = watchAnswers();

    // The recorded answer: "Echo: Name **two bike** parts.", after a reasoning of
    // "A *bike* has parts.".
    await ask(user, "Name **two bike** parts.");

    expect(await screen.findByText("two bike")).toHaveClass("font-semibold");
    expect(screen.getByRole("article", { name: "Actio" })).toHaveTextContent(
      "Echo: Name two bike parts.",
    );
    expect(seen.some((text) => text.includes("Echo: Name"))).toBe(true);
    expect(seen.filter((text) => text.includes("*"))).toEqual([]);
  });
});

describe("a conversation opened by its address", () => {
  it("is shown once the code is here, and never as written before", async () => {
    const arrive = holdCode();
    const api = fakeApi().on(CONVERSATION, stored);
    const { open } = await newPage();
    const seen = watchAnswers();

    open(`/c/${RECORDED.conversation}`);

    // The conversation and the code are asked for side by side.
    await waitFor(() => {
      expect(api.to(CONVERSATION)).toHaveLength(1);
      expect(code.fetched).toBe(1);
    });
    await afterAMoment();
    // The API has answered: the conversation waits for the code.
    expect(screen.getByText("Loading the conversation…")).toBeInTheDocument();
    expect(screen.queryByRole("article")).not.toBeInTheDocument();

    arrive();

    expect(
      await screen.findByRole("heading", { level: 2, name: "Parts" }),
    ).toBeVisible();
    expect(
      screen.queryByText("Loading the conversation…"),
    ).not.toBeInTheDocument();
    expect(seen.some((text) => text.includes("Parts"))).toBe(true);
    expect(seen.filter((text) => text.includes("##"))).toEqual([]);
  });

  it("is shown as markdown from its first render when the code came first", async () => {
    const reading = later(stored);
    const api = fakeApi().on(CONVERSATION, reading.answer);
    const { open } = await newPage();
    const seen = watchAnswers();

    open(`/c/${RECORDED.conversation}`);
    await waitFor(() => {
      expect(api.to(CONVERSATION)).toHaveLength(1);
      expect(code.fetched).toBe(1);
    });
    await act(async () => {
      await vi.dynamicImportSettled();
    });

    reading.release();

    expect(
      await screen.findByRole("heading", { level: 2, name: "Parts" }),
    ).toBeVisible();
    expect(seen.some((text) => text.includes("Parts"))).toBe(true);
    expect(seen.filter((text) => text.includes("##"))).toEqual([]);
  });

  it("is shown as written when the code is slow to come, and as markdown once it is here", async () => {
    const clock = clockAt();
    const arrive = holdCode();
    const api = fakeApi().on(CONVERSATION, stored);
    const { open } = await newPage();
    const { RENDERER_WAIT_MS } = await import("./chat-app");
    // Three seconds, as the decision's record says (ADR 0016): the slow connection it
    // was measured on needs about two for the code.
    expect(RENDERER_WAIT_MS).toBe(3 * SECOND);

    open(`/c/${RECORDED.conversation}`);
    await waitFor(() => {
      expect(api.to(CONVERSATION)).toHaveLength(1);
    });
    await afterAMoment();
    expect(screen.getByText("Loading the conversation…")).toBeInTheDocument();

    clock.advance(RENDERER_WAIT_MS);

    // A conversation the page has is not hidden behind a download that stalls. The
    // page sees the clock moved when it next looks, within a second.
    const shown = await screen.findByRole(
      "article",
      { name: "Actio" },
      { timeout: 2 * SECOND },
    );
    expect(shown).toHaveTextContent("## Parts");
    expect(
      screen.queryByRole("heading", { name: "Parts" }),
    ).not.toBeInTheDocument();

    arrive();

    expect(
      await screen.findByRole("heading", { level: 2, name: "Parts" }),
    ).toBeVisible();
    expect(
      screen.getByRole("article", { name: "Actio" }),
    ).not.toHaveTextContent("##");
  });

  it("waits from the moment the page has it, however long the API took", async () => {
    const clock = clockAt();
    const arrive = holdCode();
    const reading = later(stored);
    const api = fakeApi().on(CONVERSATION, reading.answer);
    const { open } = await newPage();
    const { RENDERER_WAIT_MS } = await import("./chat-app");

    open(`/c/${RECORDED.conversation}`);
    await waitFor(() => {
      expect(api.to(CONVERSATION)).toHaveLength(1);
    });
    // The API takes longer to answer than the whole wait. A wait that began when the
    // conversation was asked for would be over once the page has looked at the clock,
    // which it does within a second.
    clock.advance(RENDERER_WAIT_MS);
    await act(
      () =>
        new Promise<void>((resolve) => {
          setTimeout(resolve, 1.1 * SECOND);
        }),
    );

    reading.release();
    await afterAMoment();

    // The page has the conversation now, and only now does it wait for the code.
    expect(screen.getByText("Loading the conversation…")).toBeInTheDocument();
    expect(screen.queryByRole("article")).not.toBeInTheDocument();

    arrive();

    expect(
      await screen.findByRole("heading", { level: 2, name: "Parts" }),
    ).toBeVisible();
  });

  // Before a conversation waited for the code, this could not fail: it was shown as
  // written at once, whatever became of the code. It is here for the wait: one that
  // took a failed fetch for a fetch still on its way would leave this page saying
  // "Loading…", since the clock stands still and the wait can't run out.
  it("is shown as written at once when the code can't be had", async () => {
    // The clock stands still: the conversation can't be shown for having waited.
    clockAt();
    code.gone = true;
    fakeApi().on(CONVERSATION, stored);
    const { open } = await newPage();

    open(`/c/${RECORDED.conversation}`);

    expect(
      await screen.findByRole("article", { name: "Actio" }),
    ).toHaveTextContent("## Parts");
    expect(code.fetched).toBe(1);
    expect(
      screen.queryByRole("heading", { name: "Parts" }),
    ).not.toBeInTheDocument();
  });
});

/** From now on, note the whole text of every answer on the page each time the page has
 * changed: what a reader could see at once, where `watchAnswers` notes each piece that
 * came and went. */
function watchWholeAnswers(): string[][] {
  const seen: string[][] = [];
  const observer = new MutationObserver(() => {
    seen.push(
      [...document.querySelectorAll(ANSWER)].map(
        (answer) => answer.textContent,
      ),
    );
  });
  observer.observe(document.body, {
    childList: true,
    subtree: true,
    characterData: true,
  });
  onTestFinished(() => {
    observer.disconnect();
  });
  return seen;
}

/** From now on, keep whatever is written to the console as an error or a warning. */
function watchConsole(): unknown[][] {
  const said: unknown[][] = [];
  for (const kind of ["error", "warn"] as const) {
    vi.spyOn(console, kind).mockImplementation((...args: unknown[]) => {
      said.push(args);
    });
  }
  return said;
}

/** The conversation and the question of the first turn the page asked for. */
function askedIn(api: FakeApi): { conversation: string; question: string } {
  const [call] = api.to(TURNS);
  const body = call?.body as { message?: { id: string } } | undefined;
  return {
    conversation: call?.path.split("/")[3] ?? "",
    question: body?.message?.id ?? "",
  };
}

/** Let time go by, more than the second within which the page looks at the clock. */
async function pastALook(): Promise<void> {
  await act(
    () =>
      new Promise<void>((resolve) => {
        setTimeout(resolve, 1.1 * SECOND);
      }),
  );
}

const SECOND_ANSWER = "01900000-0000-7000-8000-000000000004";

// The recorded answer of `markdown.sse`: "Echo: Name **two bike** parts.", after a
// reasoning of "A *bike* has parts.". Its reasoning is its first eight events, with the
// turn's and its step's start; the first two pieces of its words come three after.
const REASONING = "A *bike* has parts.";
const WORDS = "Echo: Name **two bike** parts.";
const UP_TO_THE_REASONING = 8;
const UP_TO_ECHO_NAME = 11;

describe("an answer that arrives while the code is on its way", () => {
  it("waits for the code while its words arrive, and is markdown from its first render once the code is here", async () => {
    // The clock stands still: however long this takes, the page's wait doesn't run out.
    clockAt();
    const arrive = holdCode();
    const turn = sentByHand("markdown.sse");
    fakeApi().on(TURNS, turn.answer);
    const { ask, open } = await newPage();
    const user = open();
    await screen.findByLabelText("Message");
    const seen = watchAnswers();

    await ask(user, "Name **two bike** parts.");
    // Its reasoning, and the first of its words: "Echo: Name ".
    turn.send(UP_TO_ECHO_NAME);
    const shown = await screen.findByRole("article", { name: "Actio" });
    await afterAMoment();

    // The page asked for the code, which is held back, and the answer arrives.
    expect(code.fetched).toBe(1);
    expect(screen.getByRole("button", { name: "Stop" })).toBeVisible();
    // It says so once, for its reasoning and its words, and shows none of them.
    expect(shown).toHaveTextContent(ONLY_WAITING);
    expect(screen.getAllByText(WAITING)).toHaveLength(1);
    expect(seen.filter((text) => /Echo|bike/.test(text))).toEqual([]);

    arrive();

    // Markdown, with what it has by then, while it still arrives.
    await waitFor(() => {
      expect(shown).toHaveTextContent("Echo: Name");
    });
    expect(shown).toHaveTextContent("A bike has parts.");
    expect(screen.queryByText(WAITING)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop" })).toBeVisible();

    turn.end();

    expect(await screen.findByText("two bike")).toHaveClass("font-semibold");
    expect(shown).toHaveTextContent("Echo: Name two bike parts.");
    expect(seen.some((text) => text.includes("Echo: Name"))).toBe(true);
    expect(seen.filter((text) => text.includes("*"))).toEqual([]);
  });

  it("waits for the code when it comes onto the page complete, and is markdown once the code is here", async () => {
    // The connection breaks before any of the turn's events, and the page reads the
    // conversation again, which holds the whole answer by then. So the answer's first
    // render is complete: the chat library never held it before, and no answer is
    // being written once the request has failed. The clock stands still, so the
    // page's wait doesn't run out meanwhile.
    clockAt();
    const arrive = holdCode();
    const api = fakeApi();
    api
      .on(TURNS, () => {
        throw new TypeError("fetch failed");
      })
      .on(CONVERSATION, () => {
        const sent = askedIn(api);
        return json(
          200,
          conversation(sent.conversation, [
            question(sent.question, "Which parts?"),
            answer(
              RECORDED.answer,
              "## Parts\n\n1. The `saddle`\n2. The chain",
              sent.question,
            ),
          ]),
        );
      });
    const { ask, open } = await newPage();
    const user = open();
    await screen.findByLabelText("Message");
    const seen = watchAnswers();

    await ask(user, "Which parts?");

    const shown = await screen.findByRole("article", { name: "Actio" });
    await afterAMoment();
    expect(api.to(CONVERSATION)).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument();
    expect(shown).toHaveTextContent(ONLY_WAITING);
    expect(seen.filter((text) => text.includes("Parts"))).toEqual([]);

    arrive();

    expect(
      await within(shown).findByRole("heading", { level: 2, name: "Parts" }),
    ).toBeVisible();
    expect(seen.some((text) => text.includes("Parts"))).toBe(true);
    expect(seen.filter((text) => text.includes("##"))).toEqual([]);
  });

  it("is shown as written once the page has waited three seconds for the code, all of it at once, and a later answer at once", async () => {
    const clock = clockAt();
    const arrive = holdCode();
    const turn = sentByHand("markdown.sse");
    fakeApi().on(TURNS, turn.answer, () =>
      streamed("answer.sse", SECOND_ANSWER),
    );
    const { ask, open } = await newPage();
    const { RENDERER_WAIT_MS } = await import("./message");
    const user = open();
    await screen.findByLabelText("Message");
    const whole = watchWholeAnswers();

    await ask(user, "Name **two bike** parts.");
    // Its reasoning first: the page's wait begins with it.
    turn.send(UP_TO_THE_REASONING);
    const shown = await screen.findByRole("article", { name: "Actio" });
    await afterAMoment();
    expect(shown).toHaveTextContent(ONLY_WAITING);

    // Its words come later, and the answer ends.
    clock.advance(RENDERER_WAIT_MS - SECOND);
    turn.end();
    await screen.findByRole("button", { name: "Send" });
    // Not before three seconds by the clock: the page looks within a second.
    await pastALook();
    expect(shown).toHaveTextContent(ONLY_WAITING);

    clock.advance(SECOND);

    // Three seconds after its reasoning, not after its words: both at once, as written.
    await waitFor(
      () => {
        expect(shown).toHaveTextContent(WORDS);
      },
      { timeout: 2 * SECOND },
    );
    expect(shown).toHaveTextContent(REASONING);
    expect(
      whole.filter(
        ([text = ""]) => text.includes(REASONING) !== text.includes(WORDS),
      ),
    ).toEqual([]);

    // The page doesn't wait again while the code isn't here: the clock stands still.
    await ask(user, "Why is the sky blue?");
    expect(
      await screen.findByText("Echo: Why is the sky blue?"),
    ).toBeInTheDocument();
    expect(code.fetched).toBe(1);

    arrive();

    expect(await screen.findByText("two bike")).toHaveClass("font-semibold");
    expect(shown).not.toHaveTextContent("*");
  });

  it("counts the wait from an answer's first words, not from when the answer came onto the page", async () => {
    // A model may take long to begin, as one that has to load first. Counted from the
    // answer's arrival on the page, the wait would be spent before the first word,
    // and that word would be shown as it was written.
    const clock = clockAt();
    const arrive = holdCode();
    const turn = sentByHand("markdown.sse");
    fakeApi().on(TURNS, turn.answer);
    const { ask, open } = await newPage();
    const { RENDERER_WAIT_MS } = await import("./message");
    const user = open();
    await screen.findByLabelText("Message");
    const seen = watchAnswers();

    await ask(user, "Name **two bike** parts.");
    // The turn's start and its step's: the answer is on the page, with no word yet.
    turn.send(2);
    const shown = await screen.findByRole("article", { name: "Actio" });
    await afterAMoment();
    expect(shown).toHaveTextContent(ONLY_WAITING);
    expect(code.fetched).toBe(1);

    // The model takes longer to begin than the page waits for the code.
    clock.advance(RENDERER_WAIT_MS + SECOND);
    await pastALook();

    // Its reasoning comes: the wait begins only now.
    turn.send(UP_TO_THE_REASONING - 2);
    await pastALook();
    expect(shown).toHaveTextContent(ONLY_WAITING);
    expect(seen.filter((text) => text.includes("bike"))).toEqual([]);

    clock.advance(RENDERER_WAIT_MS);

    await waitFor(
      () => {
        expect(shown).toHaveTextContent(REASONING);
      },
      { timeout: 2 * SECOND },
    );

    turn.end();
    arrive();

    expect(await screen.findByText("two bike")).toHaveClass("font-semibold");
  });

  it("gives a conversation that is opened meanwhile what is left of the page's wait, and none once it is over", async () => {
    // One wait for the page: a stored conversation doesn't begin three seconds of its
    // own beside an answer's.
    const clock = clockAt();
    const arrive = holdCode();
    fakeApi()
      .on(LIST, () =>
        json(200, listed([summary(RECORDED.conversation, "Bike parts")])),
      )
      .on(TURNS, () => streamed("answer.sse", SECOND_ANSWER))
      .on(CONVERSATION, stored);
    const { ask, open } = await newPage();
    const { RENDERER_WAIT_MS } = await import("./message");
    const user = open();
    await screen.findByLabelText("Message");

    // An answer, complete, waits for the code: the page's wait has begun.
    await ask(user, "Why is the sky blue?");
    await screen.findByRole("button", { name: "Send" });
    const waiting = await screen.findByRole("article", { name: "Actio" });
    await afterAMoment();
    expect(waiting).toHaveTextContent(ONLY_WAITING);

    // Two of the three seconds later a stored conversation is opened from the list.
    clock.advance(RENDERER_WAIT_MS - SECOND);
    await user.click(await screen.findByRole("link", { name: "Bike parts" }));
    await pastALook();
    expect(screen.queryByText(/Parts/)).not.toBeInTheDocument();

    // It is shown, as written, when the page's wait is over: after one second more,
    // not three of its own.
    clock.advance(SECOND);
    expect(
      await screen.findByText(/## Parts/, undefined, { timeout: 2 * SECOND }),
    ).toBeInTheDocument();

    // And once the wait is over nothing waits again: opened anew with the clock still,
    // it is there at once.
    await user.click(screen.getByRole("button", { name: "New chat" }));
    await waitFor(() => {
      expect(screen.queryByText(/## Parts/)).not.toBeInTheDocument();
    });
    await user.click(screen.getByRole("link", { name: "Bike parts" }));
    expect(await screen.findByText(/## Parts/)).toBeInTheDocument();
    expect(code.fetched).toBe(1);

    arrive();

    expect(
      await screen.findByRole("heading", { level: 2, name: "Parts" }),
    ).toBeVisible();
  });

  it("shows every answer that waited once the wait is over", async () => {
    // Not only the one that began it.
    const clock = clockAt();
    const arrive = holdCode();
    fakeApi().on(
      TURNS,
      () => streamed("markdown.sse"),
      () => streamed("answer.sse", SECOND_ANSWER),
    );
    const { ask, open } = await newPage();
    const { RENDERER_WAIT_MS } = await import("./message");
    const user = open();
    await screen.findByLabelText("Message");

    await ask(user, "Name **two bike** parts.");
    await screen.findByRole("button", { name: "Send" });
    await ask(user, "Why is the sky blue?");
    await screen.findByRole("button", { name: "Send" });
    await afterAMoment();
    const answers = screen.getAllByRole("article", { name: "Actio" });
    expect(answers).toHaveLength(2);
    for (const waiting of answers) {
      expect(waiting).toHaveTextContent(ONLY_WAITING);
    }

    clock.advance(RENDERER_WAIT_MS);

    await waitFor(
      () => {
        expect(answers.map((shown) => shown.textContent)).toEqual([
          `Thought before answering${REASONING}${WORDS}`,
          "Echo: Why is the sky blue?",
        ]);
      },
      { timeout: 2 * SECOND },
    );

    arrive();

    expect(await screen.findByText("two bike")).toHaveClass("font-semibold");
  });

  it("is shown as written at once when the code turns out not to be had", async () => {
    // The clock stands still: nothing is shown for having waited.
    clockAt();
    const arrive = holdCode();
    code.gone = true;
    fakeApi().on(
      TURNS,
      () => streamed("markdown.sse"),
      () => streamed("answer.sse", SECOND_ANSWER),
    );
    const { ask, open } = await newPage();
    const user = open();
    await screen.findByLabelText("Message");

    await ask(user, "Name **two bike** parts.");
    await screen.findByRole("button", { name: "Send" });
    const first = screen.getByRole("article", { name: "Actio" });
    expect(first).toHaveTextContent(ONLY_WAITING);

    arrive(); // and its fetch fails

    await waitFor(() => {
      expect(first).toHaveTextContent(WORDS);
    });
    expect(first).toHaveTextContent(REASONING);

    // From then on an answer is shown as written from its first words.
    await ask(user, "Why is the sky blue?");
    expect(
      await screen.findByText("Echo: Why is the sky blue?"),
    ).toBeInTheDocument();
    expect(code.fetched).toBe(1);
  });

  it("waits once under React's StrictMode, also when what began the wait has left the page, and says nothing to the console", async () => {
    const said = watchConsole();
    const clock = clockAt();
    const arrive = holdCode();
    fakeApi().on(TURNS, () => streamed("markdown.sse"));
    const { ask, open } = await newPage();
    const { RENDERER_WAIT_MS } = await import("./message");
    const user = open("/", { strict: true });
    await screen.findByLabelText("Message");

    await ask(user, "Name **two bike** parts.");
    await screen.findByRole("button", { name: "Send" });
    expect(screen.getByRole("article", { name: "Actio" })).toHaveTextContent(
      ONLY_WAITING,
    );

    // Its conversation leaves the page while the answer waits, and a second later a
    // question is asked in a new one, whose answer waits too.
    await user.click(screen.getByRole("button", { name: "New chat" }));
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
    clock.advance(SECOND);
    await ask(user, "Name **two bike** parts.");
    await screen.findByRole("button", { name: "Send" });
    const shown = screen.getByRole("article", { name: "Actio" });
    expect(shown).toHaveTextContent(ONLY_WAITING);

    // Three seconds after the first answer's words, the wait is over: it was neither
    // lost when that answer left, nor begun again for the second, nor counted twice.
    clock.advance(RENDERER_WAIT_MS - SECOND);

    await waitFor(
      () => {
        expect(shown).toHaveTextContent(WORDS);
      },
      { timeout: 2 * SECOND },
    );

    arrive();

    expect(await screen.findByText("two bike")).toHaveClass("font-semibold");
    expect(said).toEqual([]);
  });
});
