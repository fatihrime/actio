// The code that shows a model's words as markdown is a piece apart from the page, which
// the page fetches ahead, once it knows whose session it has, the list of conversations
// has been read and a frame has shown it (message.tsx). An answer is never shown first
// as the marks it was written in, to be swapped for markdown a moment later (ADR 0016,
// revisions of 2026-11-21 and 2026-11-22).
//
// The page holds that code for as long as it is open. So each test here opens the app
// anew, from modules loaded anew: a page that has fetched nothing yet. The code comes
// through the factory below, which counts each time it is fetched, and gives it only
// when the test lets it arrive.
import { act, render, screen, waitFor } from "@testing-library/react";
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
  streamed,
} from "@/tests/fake-api";
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
    // answer's first words are on the page, those words would be shown as they were
    // written first.
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
