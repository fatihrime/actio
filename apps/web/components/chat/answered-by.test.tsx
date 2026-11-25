// Under an answer that a model wrote in the place of the one asked, the line of what it
// took says so, last: "answered by" and that model's title (ADR 0028, revision "a turn
// says which model answered, and the page says so"). The rule and its words are
// lib/chat/figures.ts's, and tested there; here, when the item is on the page, and that
// the models it is named from draw nothing of an answer again, when they come or when
// they are read again.
//
// The code that draws markdown is a stand-in here, which notes each text it drew once
// React has put it on the page: an answer drawn again shows there. The page holds that
// code for as long as it is open, so each test opens the app from modules loaded anew,
// as newest-first.test.tsx does.
import { act, screen, waitFor, within } from "@testing-library/react";
import { useLayoutEffect } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Conversation, MessageMetadata, Turn } from "@/lib/api/client";
import { SECOND } from "@/tests/clock";
import {
  answer,
  chained,
  conversation,
  fakeApi,
  json,
  later,
  question,
  sentByHand,
} from "@/tests/fake-api";
import { holdFrames } from "@/tests/frames";
import { recorded, RECORDED } from "@/tests/recorded";

const TURNS = "POST /api/conversations/*/turns";
const CONVERSATION = "GET /api/conversations/*";
const MODELS = "GET /api/models";

/** What the line is called, for a screen reader. */
const LINE = "What this answer took";

// The recorded answer of `stood_in.sse`, and what its turn took: the default model was
// asked, and the second one, which it names to stand in for it, wrote the answer. The
// recording's clock stands still, so the first word came at the moment of the question;
// 1,500 tokens were read and 36 written, at a credit a token.
const WORDS = "From the model that stood in.";
const TOOK = [
  "first word after 10 ms",
  "1,536 credits",
  "answered by Second model",
];
// Its events: `start`, `start-step`, `text-start` and the two pieces of its words,
// then `text-end`, `finish-step` and `finish`, and the line that ends the stream.
const UP_TO_THE_FINISH = 8;

/** How long the page takes, at the most, to read the models again while the one that
 * answers can't: it looks every five seconds. */
const NEXT_LOOK = { timeout: 8 * SECOND };

/** Each text the stand-in drew, once React had put it on the page. */
const drawn: string[] = [];

/** Stands in for `./markdown`: shows the text as it is, and notes it each time React has
 * put a drawing of it on the page. */
function StandIn({ text, className }: { text: string; className?: string }) {
  useLayoutEffect(() => {
    drawn.push(text);
  });
  return <p className={className}>{text}</p>;
}

beforeEach(() => {
  drawn.length = 0;
  // Every module is loaded anew by the next import, as on a page that was just opened,
  // with the stand-in for the code that draws markdown.
  vi.resetModules();
  vi.doMock("./markdown", () => ({ default: StandIn }));
});

/** The app, as a page that has just been opened. */
async function newPage() {
  return import("@/tests/open");
}

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

/** The recorded answer, stored. */
function stored(): Conversation {
  return conversation(RECORDED.conversation, [
    question(RECORDED.question, "Who answers?"),
    answer(
      RECORDED.answer,
      WORDS,
      RECORDED.question,
      recordedTurn("stood_in.sse"),
    ),
  ]);
}

/** The line under the answer, as a screen reader finds it. */
function line(): HTMLElement | null {
  return screen.queryByRole("list", { name: LINE });
}

/** The items of the line. */
function items(list: HTMLElement | null): HTMLElement[] {
  return list === null ? [] : within(list).getAllByRole("listitem");
}

/** What each item of the line says. */
function said(list: HTMLElement | null): string[] {
  return items(list).map((item) => item.textContent);
}

/** How many times the answer's words were drawn. */
function wordsDrawn(): number {
  return drawn.filter((text) => text === WORDS).length;
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

describe("which model wrote an answer", () => {
  it("is said of an answer that arrives last in the line, which comes a frame after its last words", async () => {
    expect(recordedTurn("stood_in.sse")).toMatchObject({
      model: "standard",
      answered_by: "second",
    });
    const answering = sentByHand("stood_in.sse");
    fakeApi()
      .on(MODELS, () => json(200, chained()))
      .on(TURNS, answering.answer);
    const { ask, open } = await newPage();
    const user = open();
    await ask(user, "Who answers?");
    answering.send(UP_TO_THE_FINISH);
    expect(await screen.findByText(WORDS)).toBeVisible();
    // From here a frame comes only when the test says so.
    const nextFrame = holdFrames();

    answering.end();

    expect(
      await screen.findByRole("button", { name: "Send" }),
    ).toBeInTheDocument();
    await afterAMoment();
    expect(line()).not.toBeInTheDocument();

    act(() => {
      nextFrame();
    });

    const took = await screen.findByRole("list", { name: LINE });
    expect(said(took)).toEqual(TOOK);
    expect(
      screen.getByRole("article", { name: "Actio" }).nextElementSibling,
    ).toBe(took);
  });

  it("is said of a stored answer at once when the models were read before it, with no frame to wait for", async () => {
    // No frame comes in this test: a stored answer never arrived on this page.
    holdFrames();
    const reading = later(() => json(200, stored()));
    const api = fakeApi()
      .on(MODELS, () => json(200, chained()))
      .on(CONVERSATION, reading.answer);
    const { open } = await newPage();
    open(`/c/${RECORDED.conversation}`);
    await waitFor(() => {
      expect(api.to(MODELS)).toHaveLength(1);
    });
    await afterAMoment();

    reading.release();

    const took = await screen.findByRole("list", { name: LINE });
    expect(said(took)).toEqual(TOOK);
  });

  it("comes when the models do, after the rest of the line, which stays as it was, and nothing of the answer is drawn again, also when they are read again", async () => {
    // The models come after the conversation, and say that the model that answers
    // can't: so the page reads them again five seconds later, and now it can.
    const models = later(() =>
      json(200, chained({ status: "unreachable", thinking: false })),
    );
    const api = fakeApi()
      .on(MODELS, models.answer, () => json(200, chained()))
      .on(CONVERSATION, () => json(200, stored()));
    const { open } = await newPage();
    open(`/c/${RECORDED.conversation}`);

    const took = await screen.findByRole("list", { name: LINE });
    expect(said(took)).toEqual(TOOK.slice(0, -1));
    const before = items(took);
    expect(wordsDrawn()).toBe(1);

    models.release();

    await waitFor(() => {
      expect(said(line())).toEqual(TOOK);
    });
    expect(
      await screen.findByText("Qwen 3.5 4B can't be reached."),
    ).toBeVisible();
    // The same list, and its first items the same elements: one was added, last.
    expect(line()).toBe(took);
    const after = items(took);
    expect(after).toHaveLength(before.length + 1);
    before.forEach((item, index) => {
      expect(after[index]).toBe(item);
    });
    expect(wordsDrawn()).toBe(1);

    await waitFor(() => {
      expect(api.to(MODELS)).toHaveLength(2);
    }, NEXT_LOOK);
    await waitFor(() => {
      expect(
        screen.queryByText("Qwen 3.5 4B can't be reached."),
      ).not.toBeInTheDocument();
    });

    expect(said(line())).toEqual(TOOK);
    expect(wordsDrawn()).toBe(1);
  }, 15_000);
});
