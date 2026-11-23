// Under an answer that has ended, one quiet line says what it took: how fast the model
// wrote, how long its first word took, and what it cost (ADR 0015, revision "an answer
// says what it took"). The rules and the words are lib/chat/figures.ts's, and tested
// there; here, when the line is on the page, and how a person and a screen reader find
// it. That it waits for the code that draws markdown is in renderer.test.tsx.
import { act, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { MessageMetadata, Turn } from "@/lib/api/client";
import {
  answer,
  conversation,
  fakeApi,
  json,
  question,
  sentByHand,
  streamed,
  turn,
} from "@/tests/fake-api";
import { ask, open } from "@/tests/open";
import { recorded, RECORDED } from "@/tests/recorded";

const TURNS = "POST /api/conversations/*/turns";
const STREAM = "GET /api/conversations/*/turns/*/stream";
const CONVERSATION = "GET /api/conversations/*";

/** What the line is called, for a screen reader. */
const LINE = "What this answer took";

// The recorded answer of `figures.sse`, and what its turn took, as the API's test
// that records it chose it: 34 tokens in the model's own 4 seconds, the first word
// 1.2 seconds after the question, 1,234 credits.
const WORDS = "Thirty-four tokens, in four seconds.";
const TOOK = ["8.5 tokens a second", "first word after 1.2 s", "1,234 credits"];
// Its events: `start`, `start-step`, `text-start` and the two pieces of its words,
// then `text-end`, `finish-step` and `finish`, and the line that ends the stream.
const UP_TO_THE_WORDS = 5;
const UP_TO_THE_FINISH = 8;

/** The turn a recorded stream ends with: what the conversation says of it once it is
 * stored. */
function recordedTurn(name: string): Turn {
  const chunks = recorded(name)
    .split("\n\n")
    .map((event) => event.replace(/^data: /, ""))
    .filter((payload) => payload.startsWith("{"))
    .map(
      (payload) =>
        JSON.parse(payload) as {
          type: string;
          messageMetadata?: MessageMetadata;
        },
    );
  const ended = chunks.find((chunk) => chunk.type === "finish")?.messageMetadata
    ?.turn;
  if (ended === undefined || ended === null) {
    throw new Error(`${name} ends with no turn`);
  }
  return ended;
}

/** The line under the answer, as a screen reader finds it. */
function line(): HTMLElement | null {
  return screen.queryByRole("list", { name: LINE });
}

/** What each item of the line says. */
function said(list: HTMLElement): string[] {
  return within(list)
    .getAllByRole("listitem")
    .map((item) => item.textContent);
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

describe("what an answer took", () => {
  it("is said under the answer once it has ended, a figure an item, as its turn's record has it", async () => {
    fakeApi().on(TURNS, () => streamed("figures.sse"));
    const user = open();

    await ask(user, "How fast was that?");

    const took = await screen.findByRole("list", { name: LINE });
    const shown = screen.getByRole("article", { name: "Actio" });
    // After the answer, not in it: the article is the model's words and nothing else.
    expect(shown).not.toContainElement(took);
    expect(shown.nextElementSibling).toBe(took);
    expect(shown.textContent).toBe(WORDS);
    // Only the figures: what stands between them says nothing to anyone.
    expect(said(took)).toEqual(TOOK);
    // The log says what is added to it, and this is no news: a screen reader finds it
    // after the answer, and isn't told of it when it appears.
    expect(took).toHaveAttribute("aria-live", "off");
    // A question says nothing of the sort: one line, for the one answer.
    expect(screen.getAllByRole("list", { name: LINE })).toHaveLength(1);
    expect(
      screen.getByRole("article", { name: "You" }).nextElementSibling,
    ).not.toBe(took);
  });

  it("is not said while the answer arrives, nor once its last chunk is in before the stream has ended", async () => {
    const answering = sentByHand("figures.sse");
    fakeApi().on(TURNS, answering.answer);
    const user = open();

    await ask(user, "How fast was that?");
    answering.send(UP_TO_THE_WORDS);
    expect(await screen.findByText(WORDS)).toBeVisible();
    expect(line()).not.toBeInTheDocument();
    // The turn as it was recorded, with every figure: the answer is still arriving.
    answering.send(UP_TO_THE_FINISH - UP_TO_THE_WORDS);
    await afterAMoment();
    expect(screen.getByRole("button", { name: "Stop" })).toBeVisible();
    expect(line()).not.toBeInTheDocument();

    answering.end();

    const took = await screen.findByRole("list", { name: LINE });
    expect(said(took)).toEqual(TOOK);
  });

  it("is said of a stored answer in the same words as of the same answer that arrived", async () => {
    fakeApi().on(CONVERSATION, () =>
      json(
        200,
        conversation(RECORDED.conversation, [
          question(RECORDED.question, "How fast was that?"),
          answer(
            RECORDED.answer,
            WORDS,
            RECORDED.question,
            recordedTurn("figures.sse"),
          ),
        ]),
      ),
    );

    open(`/c/${RECORDED.conversation}`);

    const took = await screen.findByRole("list", { name: LINE });
    expect(said(took)).toEqual(TOOK);
  });

  it("says of an answer that was stopped only what its record has", async () => {
    // Its model never said how many tokens it wrote: there is no rate. What it wrote is
    // paid for, by the server's guess, and its first word came. The recording's clock
    // stands still, so that was at the moment of the question.
    fakeApi().on(TURNS, () => streamed("stopped.sse"));
    const user = open();

    await ask(user, "Count to a hundred.");

    const took = await screen.findByRole("list", { name: LINE });
    const record = recordedTurn("stopped.sse");
    expect(record.status).toBe("stopped");
    expect(said(took)).toEqual([
      "first word after 10 ms",
      `${String(record.credits)} credits`,
    ]);
  });

  it("is no list at all for an answer whose record has no figure", async () => {
    // Given up on, a minute after its server was last heard from: what it had written
    // is its answer, and it cost nothing.
    fakeApi().on(CONVERSATION, () =>
      json(
        200,
        conversation(RECORDED.conversation, [
          question(RECORDED.question, "Name a bike part."),
          answer(
            RECORDED.answer,
            "The saddle, which",
            RECORDED.question,
            turn({
              status: "interrupted",
              finish_reason: null,
              input_tokens: null,
              cached_input_tokens: null,
              output_tokens: null,
              credits: null,
              first_word_at: null,
            }),
          ),
        ]),
      ),
    );

    open(`/c/${RECORDED.conversation}`);

    const shown = await screen.findByRole("article", { name: "Actio" });
    expect(await within(shown).findByText("The saddle, which")).toBeVisible();
    await afterAMoment();
    // Not an empty one: nothing follows the answer.
    expect(line()).not.toBeInTheDocument();
    expect(shown.nextElementSibling).toBeNull();
  });

  it("is not said of an answer that has no words, though its turn was counted", async () => {
    // Its model answered with nothing, and said what it had read: that is paid for.
    // An answer with nothing in it isn't drawn, so there is nothing to say it under:
    // the page says that the model gave no answer.
    expect(recordedTurn("no_words.sse")).toMatchObject({
      status: "completed",
      credits: 30,
    });
    fakeApi().on(TURNS, () => streamed("no_words.sse"));
    const user = open();

    await ask(user, "Say nothing.");

    expect(await screen.findByText("The model gave no answer.")).toBeVisible();
    expect(
      screen.queryByRole("article", { name: "Actio" }),
    ).not.toBeInTheDocument();
    expect(line()).not.toBeInTheDocument();
  });

  it("is not said of an answer the conversation says is still being written, until it has ended", async () => {
    // Another window asked, and this page reads along with the answer.
    const answering = sentByHand("figures.sse");
    fakeApi()
      .on(CONVERSATION, () =>
        json(
          200,
          conversation(
            RECORDED.conversation,
            [question(RECORDED.question, "How fast was that?")],
            turn({
              status: "running",
              finish_reason: null,
              input_tokens: null,
              cached_input_tokens: null,
              output_tokens: null,
              credits: null,
              first_word_at: null,
              finished_at: null,
            }),
          ),
        ),
      )
      .on(STREAM, answering.answer);

    open(`/c/${RECORDED.conversation}`);
    answering.send(UP_TO_THE_FINISH);

    expect(await screen.findByText(WORDS)).toBeVisible();
    await afterAMoment();
    expect(line()).not.toBeInTheDocument();

    answering.end();

    const took = await screen.findByRole("list", { name: LINE });
    expect(said(took)).toEqual(TOOK);
  });
});
