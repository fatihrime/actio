// Under the box where a question is written, one line says how full the model's context
// window is in this conversation (ADR 0015, revision "the page says how full the model's
// window is"). The rule and its words are lib/chat/context.ts's, and tested there; here,
// where the line stands, what a person and a screen reader find in it, and which figure
// it says while answers come and go. That nothing moves when it does is for a real
// browser to show: e2e/context.spec.ts.
import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { MessageMetadata, Turn } from "@/lib/api/client";
import {
  answer,
  conversation,
  fakeApi,
  freeModel,
  json,
  later,
  model,
  question,
  refused,
  sentByHand,
  streamed,
  turn,
} from "@/tests/fake-api";
import { ask, open } from "@/tests/open";
import { recorded, RECORDED } from "@/tests/recorded";

const TURNS = "POST /api/conversations/*/turns";
const CONVERSATION = "GET /api/conversations/*";
const MODELS = "GET /api/models";

/** A stored conversation's own ids, apart from those of the recorded streams. */
const STORED = {
  conversation: "01900000-0000-7000-8000-0000000000d1",
  question: "01900000-0000-7000-8000-0000000000d2",
  answer: "01900000-0000-7000-8000-0000000000d3",
};

// The events of `figures.sse`: `start`, `start-step`, `text-start` and the two pieces
// of its words, then `text-end`, `finish-step` and `finish`, and the line that ends the
// stream. Its turn read 1,200 tokens and wrote 34, and thought nothing.
const UP_TO_THE_WORDS = 5;

/** The meter, as a screen reader finds it. */
function meter(): HTMLElement | null {
  return screen.queryByRole("meter", { name: "Context" });
}

/** The meter's place: under the box, the composer's next sibling. */
function place(): HTMLElement {
  const form = screen.getByLabelText("Message").closest("form");
  const after = form?.nextElementSibling;
  if (!(after instanceof HTMLElement)) {
    throw new Error("nothing stands under the box");
  }
  return after;
}

/** What a person reads beside the meter: the words a screen reader isn't told, since
 * the meter tells it the same. */
function words(): HTMLElement {
  const hidden = place().querySelectorAll("[aria-hidden='true']");
  const [only] = hidden;
  if (hidden.length !== 1 || !(only instanceof HTMLElement)) {
    throw new Error(
      "no words, or more than one place for them, beside the meter",
    );
  }
  return only;
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

/** A stored conversation of one question, answered by a turn after which it takes
 * `tokens` of the window. */
function answeredWith(tokens: number, changes: Partial<Turn> = {}) {
  return json(
    200,
    conversation(STORED.conversation, [
      question(STORED.question, "Name a bike part."),
      answer(
        STORED.answer,
        "The saddle.",
        STORED.question,
        turn({ context_tokens: tokens, ...changes }),
      ),
    ]),
  );
}

describe("how full the model's window is", () => {
  it("has its place under the box from the start, and says the recorded figure once an answer has ended", async () => {
    fakeApi().on(TURNS, () => streamed("answer.sse"));
    const user = open();
    await screen.findByLabelText("Message");

    // A new chat: the place, and nothing in it.
    expect(place()).toBeEmptyDOMElement();
    expect(meter()).not.toBeInTheDocument();

    await ask(user, "Why is the sky blue?");

    // 30 tokens read and 6 written, at the end of a conversation of one question.
    const shown = await screen.findByRole("meter", { name: "Context" });
    expect(place()).toContainElement(shown);
    expect(shown).toHaveAttribute("aria-valuemin", "0");
    expect(shown).toHaveAttribute("aria-valuemax", "100");
    expect(shown).toHaveAttribute("aria-valuenow", "0");
    expect(shown).toHaveAttribute(
      "aria-valuetext",
      "under 1% full: 36 of 8,192 tokens",
    );
    // The words a person reads repeat what the meter says: a screen reader is told it
    // once, by the meter, which the word before it names.
    expect(words()).toHaveTextContent(/^under 1% full · 36 of 8,192 tokens$/);
    expect(within(place()).getByText("Context")).not.toHaveAttribute(
      "aria-hidden",
    );
    // Outside the log, and in no live region: a new figure is no news.
    expect(screen.getByRole("log")).not.toContainElement(shown);
    expect(
      place().closest("[aria-live], [role='status'], [role='log']"),
    ).toBeNull();
    expect(within(place()).queryByRole("status")).not.toBeInTheDocument();
  });

  it("says a stored conversation's newest figure as soon as the conversation is shown", async () => {
    fakeApi().on(CONVERSATION, () => answeredWith(3_214));

    open(`/c/${STORED.conversation}`);

    await screen.findByText("The saddle.");
    expect(meter()).toHaveAttribute(
      "aria-valuetext",
      "39% full: 3,214 of 8,192 tokens",
    );
    expect(meter()).toHaveAttribute("aria-valuenow", "39");
    expect(words()).toHaveTextContent(/^39% full · 3,214 of 8,192 tokens$/);
  });

  it("says the server's figure of an answer that thought, not what the model read and wrote together", async () => {
    // The thought isn't kept for the next question.
    const record = recordedTurn("reasoning.sse");
    expect(record).toMatchObject({ input_tokens: 35, output_tokens: 10 });
    expect(record.context_tokens).toBe(40);
    fakeApi().on(TURNS, () => streamed("reasoning.sse"));
    const user = open();

    await ask(user, "Why is the sky blue?");

    await waitFor(() => {
      expect(meter()).toHaveAttribute(
        "aria-valuetext",
        "under 1% full: 40 of 8,192 tokens",
      );
    });
  });

  it("says the figure from before while an answer arrives, and the answer's once its turn has ended", async () => {
    const answering = sentByHand("figures.sse");
    fakeApi()
      .on(CONVERSATION, () => answeredWith(3_214))
      .on(TURNS, answering.answer);
    const user = open(`/c/${STORED.conversation}`);
    await screen.findByText("The saddle.");

    await ask(user, "How fast was that?");
    answering.send(UP_TO_THE_WORDS);

    expect(
      await screen.findByText("Thirty-four tokens, in four seconds."),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Stop" })).toBeVisible();
    expect(meter()).toHaveAttribute(
      "aria-valuetext",
      "39% full: 3,214 of 8,192 tokens",
    );

    answering.end();

    await waitFor(() => {
      expect(meter()).toHaveAttribute(
        "aria-valuetext",
        "15% full: 1,234 of 8,192 tokens",
      );
    });
  });

  it("keeps the figure from before an answer that was stopped: nobody counted it", async () => {
    expect(recordedTurn("stopped.sse").context_tokens).toBeNull();
    fakeApi()
      .on(CONVERSATION, () => answeredWith(3_214))
      .on(TURNS, () => streamed("stopped.sse"));
    const user = open(`/c/${STORED.conversation}`);
    await screen.findByText("The saddle.");

    await ask(user, "Count to a hundred.");

    await screen.findByText("One, two, three");
    expect(
      await screen.findByRole("button", { name: "Send" }),
    ).toBeInTheDocument();
    expect(meter()).toHaveAttribute(
      "aria-valuetext",
      "39% full: 3,214 of 8,192 tokens",
    );
  });

  it("says the figure of a turn that completed and left its question unanswered", async () => {
    // Its model wrote nothing, and said what it had read: that is what the
    // conversation takes. Stored, the question stands open, with that turn.
    const record = recordedTurn("no_words.sse");
    expect(record).toMatchObject({ status: "completed", context_tokens: 30 });
    fakeApi().on(CONVERSATION, () =>
      json(
        200,
        conversation(
          RECORDED.conversation,
          [question(RECORDED.question, "Say nothing.")],
          record,
        ),
      ),
    );

    open(`/c/${RECORDED.conversation}`);

    expect(await screen.findByText("The model gave no answer.")).toBeVisible();
    expect(meter()).toHaveAttribute(
      "aria-valuetext",
      "under 1% full: 30 of 8,192 tokens",
    );
  });

  it("says the same of that turn as it arrives", async () => {
    fakeApi().on(TURNS, () => streamed("no_words.sse"));
    const user = open();

    await ask(user, "Say nothing.");

    expect(await screen.findByText("The model gave no answer.")).toBeVisible();
    expect(meter()).toHaveAttribute(
      "aria-valuetext",
      "under 1% full: 30 of 8,192 tokens",
    );
  });

  it("says from 90% on that a new chat is due soon, in the caution colour", async () => {
    fakeApi().on(CONVERSATION, () => answeredWith(7_640));

    open(`/c/${STORED.conversation}`);

    await screen.findByText("The saddle.");
    const shown = meter();
    expect(shown).toHaveAttribute("aria-valuenow", "93");
    expect(shown).toHaveAttribute(
      "aria-valuetext",
      "93% full: 7,640 of 8,192 tokens. Start a new chat soon.",
    );
    expect(words()).toHaveTextContent(
      /^93% full · 7,640 of 8,192 tokens · start a new chat soon$/,
    );
    expect(words()).toHaveClass("text-caution-foreground");
    expect(shown?.firstElementChild).toHaveClass("bg-caution");
  });

  it("says nothing of a new chat being due below 90%", async () => {
    fakeApi().on(CONVERSATION, () => answeredWith(7_372)); // 89.99%

    open(`/c/${STORED.conversation}`);

    await screen.findByText("The saddle.");
    expect(meter()).toHaveAttribute(
      "aria-valuetext",
      "89% full: 7,372 of 8,192 tokens",
    );
    // The value is the whole percent that is full, as the words say it: not the
    // nearest one, which would be 90.
    expect(meter()).toHaveAttribute("aria-valuenow", "89");
    expect(words()).toHaveTextContent(/^89% full · 7,372 of 8,192 tokens$/);
    expect(words()).not.toHaveClass("text-caution-foreground");
    expect(meter()?.firstElementChild).toHaveClass("bg-primary");
  });

  it.each([
    ["a window that is full", 8_192],
    ["a figure over the window", 9_000],
  ])("says no more than 100%% full, for %s", async (_, tokens) => {
    fakeApi().on(CONVERSATION, () => answeredWith(tokens));

    open(`/c/${STORED.conversation}`);

    await screen.findByText("The saddle.");
    const shown = meter();
    expect(shown).toHaveAttribute("aria-valuenow", "100");
    expect(shown).toHaveAttribute(
      "aria-valuetext",
      `100% full: ${tokens.toLocaleString("en")} of 8,192 tokens. Start a new chat soon.`,
    );
    expect(shown?.firstElementChild).toHaveStyle({ width: "100%" });
  });

  it("goes when the model that answers is another than the one the figure is of", async () => {
    // At a usage limit, the model that costs nothing carries on. The figure is the
    // usual model's: those are tokens of another window.
    fakeApi()
      .on(MODELS, () =>
        json(200, { default: "standard", models: [model(), freeModel()] }),
      )
      .on(CONVERSATION, () => answeredWith(3_214))
      .on(TURNS, () => refused("limit_reached.json"));
    const user = open(`/c/${STORED.conversation}`);
    await screen.findByText("The saddle.");
    expect(meter()).toBeInTheDocument();

    await ask(user, "And another?");
    await user.click(
      await screen.findByRole("button", { name: "Use Local (free)" }),
    );

    expect(
      await screen.findByText("Local is answering, at no cost."),
    ).toBeInTheDocument();
    expect(meter()).not.toBeInTheDocument();
    expect(place()).toBeEmptyDOMElement();
  });

  it("says nothing until the models have been read, which say how large the window is", async () => {
    const models = later(() =>
      json(200, { default: "standard", models: [model()] }),
    );
    fakeApi()
      .on(MODELS, models.answer)
      .on(CONVERSATION, () => answeredWith(3_214));

    open(`/c/${STORED.conversation}`);

    await screen.findByText("The saddle.");
    expect(meter()).not.toBeInTheDocument();

    models.release();

    expect(
      await screen.findByRole("meter", { name: "Context" }),
    ).toHaveAttribute("aria-valuetext", "39% full: 3,214 of 8,192 tokens");
  });
});
