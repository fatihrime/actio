// Under an answer that a model wrote in the place of the one asked, the line of what it
// took says so, last: "answered by" that model's title "in place of" the asked one's
// (ADR 0028, revision "a turn says which model answered, and the page says so"). And
// where more than one model is on offer, it names the model that wrote an answer of its
// own too, "answered by" and its title (ADR 0032, revision "an answer says which model
// wrote it, where there is a choice"). The rule and its words are lib/chat/figures.ts's,
// and tested there; here, when the item is on the page, and that the models it is named
// from draw nothing of an answer again, when they come or when they are read again.
//
// The code that draws markdown is a stand-in here, which notes each text it drew once
// React has put it on the page: an answer drawn again shows there. The page holds that
// code for as long as it is open, so each test opens the app from modules loaded anew,
// as newest-first.test.tsx does.
import { act, screen, waitFor, within } from "@testing-library/react";
import { useLayoutEffect } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  Conversation,
  MessageMetadata,
  ModelList,
  Turn,
} from "@/lib/api/client";
import { SECOND } from "@/tests/clock";
import {
  answer,
  chained,
  conversation,
  fakeApi,
  freeModel,
  json,
  later,
  model,
  question,
  sentByHand,
  turn,
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
  "answered by Second model in place of Qwen 3.5 4B",
];
// Its events: `start`, `start-step`, `text-start` and the two pieces of its words,
// then `text-end`, `finish-step` and `finish`, and the line that ends the stream. So
// are `figures.sse`'s.
const UP_TO_THE_FINISH = 8;

// The recorded answer of `figures.sse`, and what its turn took: the default model was
// asked, and wrote the answer itself, 34 tokens in its own 4 seconds, the first word
// 1.2 seconds after the question, 1,234 credits.
const OWN_WORDS = "Thirty-four tokens, in four seconds.";
const OWN_TOOK = [
  "8.5 tokens a second",
  "first word after 1.2 s",
  "1,234 credits",
  "answered by Qwen 3.5 4B",
];

/** A stored conversation's own ids, apart from those of the recorded streams. */
const STORED = {
  question: "01900000-0000-7000-8000-0000000000d2",
  answer: "01900000-0000-7000-8000-0000000000d3",
  turn: "01900000-0000-7000-8000-0000000000d4",
};
/** The words of the stored conversation's second answer, which Local wrote. */
const LOCAL_WORDS = "From the model chosen next.";

/** What `GET /api/models` answers where two models are on offer, neither of which
 * stands in for the other: the default one, and Local. */
function twoModels(): ModelList {
  return { default: "standard", models: [model(), freeModel()] };
}

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

/** A stored conversation of two answers, each by the model that was asked for it: the
 * recorded answer of `figures.sse`, by the default model, and a second one by Local,
 * which cost nothing. */
function twoAnswers(): Conversation {
  return conversation(RECORDED.conversation, [
    question(RECORDED.question, "How fast was that?"),
    answer(
      RECORDED.answer,
      OWN_WORDS,
      RECORDED.question,
      recordedTurn("figures.sse"),
    ),
    question(STORED.question, "And with the other one?", RECORDED.answer),
    answer(
      STORED.answer,
      LOCAL_WORDS,
      STORED.question,
      turn({ id: STORED.turn, model: "local", credits: 0 }),
    ),
  ]);
}

/** What the line under the second answer of `twoAnswers` says: its record's clock
 * stands still, so the first word came at the moment of the question. */
const LOCAL_TOOK = ["first word after 10 ms", "0 credits", "answered by Local"];

/** The line under the answer, as a screen reader finds it. */
function line(): HTMLElement | null {
  return screen.queryByRole("list", { name: LINE });
}

/** The lines under the answers, from the top. */
function lines(): HTMLElement[] {
  return screen.queryAllByRole("list", { name: LINE });
}

/** The items of the line. */
function items(list: HTMLElement | null): HTMLElement[] {
  return list === null ? [] : within(list).getAllByRole("listitem");
}

/** What each item of the line says. */
function said(list: HTMLElement | null): string[] {
  return items(list).map((item) => item.textContent);
}

/** How many times an answer's words were drawn: the recorded answer's of
 * `stood_in.sse`, unless `words` says whose. */
function wordsDrawn(words = WORDS): number {
  return drawn.filter((text) => text === words).length;
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

  it("is said of the asked model's own answer that arrives, where another model is on offer, last in the line, which comes a frame after its last words", async () => {
    expect(recordedTurn("figures.sse")).toMatchObject({
      model: "standard",
      answered_by: "standard",
    });
    const answering = sentByHand("figures.sse");
    fakeApi()
      .on(MODELS, () => json(200, twoModels()))
      .on(TURNS, answering.answer);
    const { ask, open } = await newPage();
    const user = open();
    await ask(user, "How fast was that?");
    answering.send(UP_TO_THE_FINISH);
    expect(await screen.findByText(OWN_WORDS)).toBeVisible();
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
    expect(said(took)).toEqual(OWN_TOOK);
    expect(
      screen.getByRole("article", { name: "Actio" }).nextElementSibling,
    ).toBe(took);
  });

  it("is said under each answer of a stored conversation that two models on offer wrote, by the one that wrote it, at once when the models were read before it", async () => {
    // No frame comes in this test: a stored answer never arrived on this page.
    holdFrames();
    const reading = later(() => json(200, twoAnswers()));
    const api = fakeApi()
      .on(MODELS, () => json(200, twoModels()))
      .on(CONVERSATION, reading.answer);
    const { open } = await newPage();
    open(`/c/${RECORDED.conversation}`);
    await waitFor(() => {
      expect(api.to(MODELS)).toHaveLength(1);
    });
    await afterAMoment();

    reading.release();

    const [first, second] = await screen.findAllByRole("list", { name: LINE });
    expect(said(first ?? null)).toEqual(OWN_TOOK);
    expect(said(second ?? null)).toEqual(LOCAL_TOOK);
    // Each the next sibling of the answer it is about.
    const answers = screen.getAllByRole("article", { name: "Actio" });
    expect(answers).toHaveLength(2);
    expect(answers.map((shown) => shown.nextElementSibling)).toEqual([
      first,
      second,
    ]);
  });

  it("comes under the asked models' own answers when the models do, after the rest of each line, which stays as it was, and draws neither answer's words again", async () => {
    const models = later(() => json(200, twoModels()));
    fakeApi()
      .on(MODELS, models.answer)
      .on(CONVERSATION, () => json(200, twoAnswers()));
    const { open } = await newPage();
    open(`/c/${RECORDED.conversation}`);

    await waitFor(() => {
      expect(lines()).toHaveLength(2);
    });
    const before = lines();
    expect(before.map(said)).toEqual([
      OWN_TOOK.slice(0, -1),
      LOCAL_TOOK.slice(0, -1),
    ]);
    const itemsBefore = before.map(items);
    expect(wordsDrawn(OWN_WORDS)).toBe(1);
    expect(wordsDrawn(LOCAL_WORDS)).toBe(1);

    models.release();

    await waitFor(() => {
      expect(lines().map(said)).toEqual([OWN_TOOK, LOCAL_TOOK]);
    });
    // The same lists, and their first items the same elements: one was added to each,
    // last.
    const after = lines();
    after.forEach((list, index) => {
      expect(list).toBe(before[index]);
      const shown = items(list);
      const earlier = itemsBefore[index] ?? [];
      expect(shown).toHaveLength(earlier.length + 1);
      earlier.forEach((item, at) => {
        expect(shown[at]).toBe(item);
      });
    });
    expect(wordsDrawn(OWN_WORDS)).toBe(1);
    expect(wordsDrawn(LOCAL_WORDS)).toBe(1);
  });

  it("is not said of an answer kept from a turn that was given up on, whose record names no model, where two are on offer", async () => {
    // What the API says of a turn it gave up on: nothing counted, no first word, and no
    // model that wrote the words it kept. A model asked in another's place may have.
    const KEPT = "As far as it got.";
    const givenUp = turn({
      id: STORED.turn,
      status: "interrupted",
      finish_reason: null,
      answered_by: null,
      input_tokens: null,
      cached_input_tokens: null,
      output_tokens: null,
      credits: null,
      first_word_at: null,
    });
    fakeApi()
      .on(MODELS, () => json(200, twoModels()))
      .on(CONVERSATION, () =>
        json(
          200,
          conversation(RECORDED.conversation, [
            question(RECORDED.question, "How fast was that?"),
            answer(
              RECORDED.answer,
              OWN_WORDS,
              RECORDED.question,
              recordedTurn("figures.sse"),
            ),
            question(STORED.question, "And then?", RECORDED.answer),
            answer(STORED.answer, KEPT, STORED.question, givenUp),
          ]),
        ),
      );
    const { open } = await newPage();
    open(`/c/${RECORDED.conversation}`);

    // The first answer's line names its model: the models are read.
    await waitFor(() => {
      expect(lines().map(said)).toEqual([OWN_TOOK]);
    });
    const answers = screen.getAllByRole("article", { name: "Actio" });
    expect(answers).toHaveLength(2);
    expect(answers[1]).toHaveTextContent(KEPT);
    await afterAMoment();

    // No line under the answer that was kept: its record has no figure, and the model
    // asked isn't said to have written it.
    expect(lines().map(said)).toEqual([OWN_TOOK]);
    expect(answers[0]?.nextElementSibling).toBe(lines()[0]);
  });
});
