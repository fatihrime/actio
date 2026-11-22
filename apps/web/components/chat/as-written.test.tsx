// A model's words whose markdown can't be drawn are shown as they were written, and
// nothing else on the page goes with them (ADR 0016, revision "an answer that can't be
// drawn is shown as written"): a part of the library that couldn't be fetched, or a
// mistake of the library on some text a model wrote, throws while the page is drawn.
//
// The code that draws markdown is a stand-in here, which throws for the text each test
// chooses, and shows any other text after "Drawn: ". The page holds that code for as
// long as it is open, so each test opens the app from modules loaded anew, as
// renderer.test.tsx does. React writes each error it caught to the console: the tests
// keep what it writes, and say what it should be.
import { act, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, onTestFinished, vi } from "vitest";

import { clockAt } from "@/tests/clock";
import type { Clock } from "@/tests/clock";
import {
  answer,
  conversation,
  fakeApi,
  json,
  question,
  sentByHand,
  streamed,
} from "@/tests/fake-api";
import { RECORDED } from "@/tests/recorded";

import { DRAWN_EVERY_MS } from "./paced";

const TURNS = "POST /api/conversations/*/turns";
const CONVERSATION = "GET /api/conversations/*";
const SECOND_QUESTION = "01900000-0000-7000-8000-000000000003";
const SECOND_ANSWER = "01900000-0000-7000-8000-000000000004";

/** What the stand-in throws. */
class CantDraw extends Error {}

const drawing: {
  /** How many times the page fetched the code that draws markdown. */
  fetched: number;
  /** Whether the stand-in throws for a text. */
  fails: (text: string) => boolean;
  /** Every text the stand-in was given, in order. */
  given: string[];
} = { fetched: 0, fails: () => false, given: [] };

/** Stands in for `./markdown`: throws for the text the test chose, and shows any
 * other text, after "Drawn: ". */
function StandIn({ text, className }: { text: string; className?: string }) {
  drawing.given.push(text);
  if (drawing.fails(text)) {
    throw new CantDraw(`Can't draw: ${text}`);
  }
  return <p className={className}>{`Drawn: ${text}`}</p>;
}

beforeEach(() => {
  drawing.fetched = 0;
  drawing.fails = () => false;
  drawing.given = [];
  // Every module is loaded anew by the next import, as on a page that was just opened.
  vi.resetModules();
  vi.doMock("./markdown", () => {
    drawing.fetched += 1;
    return { default: StandIn };
  });
});

/** From now on, keep what is written to the console as an error. What React says of
 * each error it caught carries that error: the stand-in's are handed back, one for
 * each time it threw. Anything else written there fails the test. */
function watchReports(): CantDraw[] {
  const reports: CantDraw[] = [];
  const said: unknown[][] = [];
  vi.spyOn(console, "error").mockImplementation((...args: unknown[]) => {
    const thrown = args.find((arg) => arg instanceof CantDraw);
    if (thrown === undefined) {
      said.push(args);
    } else {
      reports.push(thrown);
    }
  });
  onTestFinished(() => {
    expect(said, "what else was written to the console as an error").toEqual(
      [],
    );
  });
  return reports;
}

/** The app, as a page that has just been loaded. */
async function newPage() {
  return import("@/tests/open");
}

/** Open a new chat, and wait until the code that draws markdown is here: from then on
 * an answer is drawn from its first piece. */
async function openWithRenderer() {
  const page = await newPage();
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
  const answers = screen.getAllByRole("article", { name: "Actio" });
  const newest = answers.at(-1);
  if (newest === undefined) {
    throw new Error("No answer is on the page.");
  }
  return newest;
}

/** Wait until the newest answer holds exactly `text`, and nothing else. */
async function shows(text: string): Promise<void> {
  await waitFor(() => {
    expect(newestAnswer().textContent).toBe(text);
  });
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

// The recorded answer of `answer.sse`, as it arrives: "Echo: ", then a word at a time.
// Its first three events (the turn's start, its step's, its text's) come before.
const WHOLE = "Echo: Why is the sky blue?";
const PIECES = ["Echo: ", "Why ", "is ", "the ", "sky ", "blue?"];
const BEFORE_THE_TEXT = 3;

describe("an answer whose markdown can't be drawn", () => {
  it("is shown as it was written, and the page goes on", async () => {
    const reports = watchReports();
    // The recorded answer of `markdown.sse`: "Echo: Name **two bike** parts.", after
    // a reasoning of "A *bike* has parts.".
    drawing.fails = (text) => text.startsWith("Echo: Name");
    fakeApi().on(
      TURNS,
      () => streamed("markdown.sse"),
      () => streamed("answer.sse", SECOND_ANSWER),
    );
    const { ask, user } = await openWithRenderer();

    await ask(user, "Name **two bike** parts.");

    const words = await within(newestAnswer()).findByText(
      "Echo: Name **two bike** parts.",
    );
    expect(words.textContent).toBe("Echo: Name **two bike** parts.");
    // Its lines as they were, as before the code that draws markdown was here.
    expect(words).toHaveClass("whitespace-pre-wrap");
    expect(newestAnswer()).not.toHaveTextContent("Drawn: Echo");
    expect(reports).not.toEqual([]);

    // The box is there, and takes the next question, whose answer is drawn.
    await screen.findByRole("button", { name: "Send" });
    await ask(user, "Why is the sky blue?");

    expect(await screen.findByText(`Drawn: ${WHOLE}`)).toBeInTheDocument();
    expect(screen.getAllByRole("article", { name: "You" })).toHaveLength(2);
    const [first] = screen.getAllByRole("article", { name: "Actio" });
    expect(first?.textContent).toContain("Echo: Name **two bike** parts.");
  });

  it("shows each piece as it arrives, and isn't tried again at each", async () => {
    const reports = watchReports();
    drawing.fails = (text) => text.startsWith("Echo: ");
    const turn = sentByHand("answer.sse");
    fakeApi().on(TURNS, turn.answer);
    const { ask, user } = await openWithRenderer();

    await ask(user, "Why is the sky blue?");
    turn.send(BEFORE_THE_TEXT + 1);

    await shows("Echo: ");
    expect(reports).toHaveLength(1);
    const tried = drawing.given.length;
    let arrived = PIECES[0] ?? "";
    for (const piece of PIECES.slice(1)) {
      turn.send();
      arrived += piece;
      await shows(arrived);
    }

    // Each piece is on the page, as written, and none after the first, which threw,
    // was given to the code that draws markdown.
    expect(arrived).toBe(WHOLE);
    expect(drawing.given).toHaveLength(tried);
    expect(reports).toHaveLength(1);

    turn.end();
    await screen.findByRole("button", { name: "Send" });
  });

  it.each([
    {
      outcome: "drawn, when only half of it threw",
      // Half of something can throw where the whole doesn't.
      fails: (text: string) => text.startsWith("Echo: ") && text !== WHOLE,
      shown: `Drawn: ${WHOLE}`,
      reported: 1,
    },
    {
      outcome: "as written, when the whole throws too",
      fails: (text: string) => text.startsWith("Echo: "),
      shown: WHOLE,
      reported: 2,
    },
  ])(
    "is tried once more when it is complete: $outcome",
    async ({ fails, shown, reported }) => {
      const reports = watchReports();
      drawing.fails = fails;
      const turn = sentByHand("answer.sse");
      fakeApi().on(TURNS, turn.answer);
      const { ask, user } = await openWithRenderer();

      await ask(user, "Why is the sky blue?");
      turn.send(BEFORE_THE_TEXT + 1);
      await shows("Echo: ");
      expect(reports).toHaveLength(1);

      // The rest of it, and the turn's end.
      turn.end();

      await screen.findByRole("button", { name: "Send" });
      await shows(shown);
      expect(drawing.given).toContain(WHOLE);
      await afterAMoment();
      // Tried once at its end, and no more.
      expect(reports).toHaveLength(reported);
      expect(newestAnswer().textContent).toBe(shown);
    },
  );

  it("is shown as written when it is stored, beside the rest as ever", async () => {
    const reports = watchReports();
    drawing.fails = (text) => text === "The **saddle**, then the chain.";
    fakeApi().on(CONVERSATION, () =>
      json(
        200,
        conversation(RECORDED.conversation, [
          question(RECORDED.question, "Which parts?"),
          answer(RECORDED.answer, "## Parts", RECORDED.question),
          question(SECOND_QUESTION, "Which **first**?", RECORDED.answer),
          answer(
            SECOND_ANSWER,
            "The **saddle**, then the chain.",
            SECOND_QUESTION,
          ),
        ]),
      ),
    );
    const { open } = await newPage();

    open(`/c/${RECORDED.conversation}`);

    expect(await screen.findByText("Drawn: ## Parts")).toBeInTheDocument();
    const [drawn, asWritten] = screen.getAllByRole("article", {
      name: "Actio",
    });
    expect(drawn?.textContent).toBe("Drawn: ## Parts");
    expect(asWritten?.textContent).toBe("The **saddle**, then the chain.");
    expect(
      screen
        .getAllByRole("article", { name: "You" })
        .map((asked) => asked.textContent),
    ).toEqual(["Which parts?", "Which **first**?"]);
    expect(screen.getByLabelText("Message")).toBeVisible();
    // Tried once: it never arrives, so nothing completes it for another try.
    await afterAMoment();
    expect(reports).toHaveLength(1);
  });
});

describe("an answer's reasoning and its words", () => {
  // The recorded answer of `markdown.sse`.
  const REASONING = "A *bike* has parts.";
  const WORDS = "Echo: Name **two bike** parts.";

  it.each([
    {
      which: "the reasoning can't be drawn, and the words are",
      fails: REASONING,
      drawn: WORDS,
    },
    {
      which: "the words can't be drawn, and the reasoning is",
      fails: WORDS,
      drawn: REASONING,
    },
  ])("are drawn apart: $which", async ({ fails, drawn }) => {
    const reports = watchReports();
    // It throws for any piece of the one, half-arrived or whole.
    drawing.fails = (text) => text !== "" && fails.startsWith(text);
    fakeApi().on(TURNS, () => streamed("markdown.sse"));
    const { ask, user } = await openWithRenderer();

    await ask(user, "Name **two bike** parts.");

    const shown = within(await screen.findByRole("article", { name: "Actio" }));
    expect(await shown.findByText(`Drawn: ${drawn}`)).toBeInTheDocument();
    await screen.findByRole("button", { name: "Send" });
    expect(shown.getByText(fails).textContent).toBe(fails);
    expect(shown.queryByText(`Drawn: ${fails}`)).not.toBeInTheDocument();
    expect(reports).not.toEqual([]);
  });
});

// Words shown as written arrive at the pace of any answer (ADR 0016, revision "an
// arriving answer is drawn at most every 50 ms"): as written, a long answer is one long
// paragraph, which the browser lays out again at each change.
describe("an answer that is shown as written while it arrives", () => {
  /** The first of its words are on the page at once. The next two pieces aren't while
   * the test's clock stands still, however long, and are once it has moved on. Whole
   * when it ends. */
  async function keepsThePace(
    turn: ReturnType<typeof sentByHand>,
    clock: Clock,
  ): Promise<void> {
    turn.send(BEFORE_THE_TEXT + 1);
    await shows("Echo: ");
    turn.send(2);
    await afterAMoment();
    expect(newestAnswer().textContent).toBe("Echo: ");

    clock.advance(DRAWN_EVERY_MS);

    await shows("Echo: Why is ");
    turn.end();
    await screen.findByRole("button", { name: "Send" });
    await shows(WHOLE);
  }

  it("keeps the pace of an arriving answer when its markdown can't be drawn", async () => {
    watchReports();
    const clock = clockAt();
    drawing.fails = (text) => text.startsWith("Echo: ");
    const turn = sentByHand("answer.sse");
    fakeApi().on(TURNS, turn.answer);
    const { ask, user } = await openWithRenderer();

    await ask(user, "Why is the sky blue?");

    await keepsThePace(turn, clock);
  });

  it("keeps the pace of an arriving answer when the code that draws markdown can't be had", async () => {
    const clock = clockAt();
    vi.doMock("./markdown", () => {
      drawing.fetched += 1;
      throw new Error("Failed to load chunk");
    });
    const turn = sentByHand("answer.sse");
    fakeApi().on(TURNS, turn.answer);
    // The fetch of that code is over, and failed: answers are shown as written.
    const { ask, user } = await openWithRenderer();

    await ask(user, "Why is the sky blue?");

    await keepsThePace(turn, clock);
    expect(drawing.given).toEqual([]);
  });
});
