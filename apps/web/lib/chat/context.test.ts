import { describe, expect, it } from "vitest";

import type { StoredMessage, Turn } from "@/lib/api/client";
import { answer, model, question, turn } from "@/tests/fake-api";

import { contextFill, NEARLY_FULL } from "./context";
import { fromStored } from "./messages";
import type { ChatMessage } from "./messages";

/** The window of the fake API's model. */
const WINDOW = model().context_window;

/** A conversation as the page holds it: a question, then an answer by each of `turns`,
 * in turn. */
function conversation(...turns: Turn[]): ChatMessage[] {
  const stored: StoredMessage[] = turns.flatMap((by, index) => [
    question(`q${String(index)}`, "A question"),
    answer(`a${String(index)}`, "An answer", `q${String(index)}`, by),
  ]);
  return fromStored(stored);
}

/** A turn whose conversation takes `tokens` of the window after it. */
function taking(tokens: number | null, changes: Partial<Turn> = {}): Turn {
  return turn({ context_tokens: tokens, ...changes });
}

/** What the meter says of a conversation that takes `tokens` after its one answer. */
function said(tokens: number, window = WINDOW) {
  return contextFill(
    conversation(taking(tokens)),
    null,
    model({ context_window: window }),
  );
}

describe("contextFill", () => {
  it("says how full the window is, by the newest answer's figure", () => {
    const messages = conversation(taking(1_000), taking(3_214));

    expect(contextFill(messages, null, model())).toEqual({
      percent: (100 * 3_214) / 8_192,
      share: "39% full",
      count: "3,214 of 8,192 tokens",
      nearlyFull: false,
    });
  });

  it("says nothing of a conversation that has no figure yet", () => {
    expect(contextFill([], null, model())).toBeNull();
    expect(
      contextFill(fromStored([question("q0", "A question")]), null, model()),
    ).toBeNull();
  });

  it("reads the server's figure, and no other count of a turn", () => {
    // A model that thought: the two counts together are more than what the
    // conversation keeps of it.
    const thought = taking(40, { input_tokens: 35, output_tokens: 10 });

    expect(contextFill(conversation(thought), null, model())?.count).toBe(
      "40 of 8,192 tokens",
    );
  });

  it("keeps the figure from before a turn that has none: running, stopped or failed", () => {
    for (const status of ["running", "stopped", "failed"] as const) {
      const messages = conversation(
        taking(3_214),
        taking(null, { status, input_tokens: null, output_tokens: null }),
      );

      expect(contextFill(messages, null, model())?.count).toBe(
        "3,214 of 8,192 tokens",
      );
    }
  });

  it("takes the figure of the turn that left a question open, before the answers'", () => {
    // It completed, and its model wrote nothing: what it read is what is taken.
    const messages = [
      ...conversation(taking(3_214)),
      ...fromStored([question("q1", "And another?", "a0")]),
    ];

    expect(contextFill(messages, taking(3_300), model())?.count).toBe(
      "3,300 of 8,192 tokens",
    );
    // An open turn without a figure, as one that failed or is still running is, says
    // none: the answer's stands.
    expect(contextFill(messages, taking(null), model())?.count).toBe(
      "3,214 of 8,192 tokens",
    );
  });

  it("takes no open turn of a conversation that ends with an answer", () => {
    // Read before the answer came: what the page held of the question that was open.
    const messages = conversation(taking(3_214));

    expect(contextFill(messages, taking(3_300), model())?.count).toBe(
      "3,214 of 8,192 tokens",
    );
  });

  it("says nothing before the model that answers is known", () => {
    expect(contextFill(conversation(taking(3_214)), null, null)).toBeNull();
  });

  it("says nothing of another model's figure, those are another window's tokens", () => {
    const local = model({ id: "local", context_window: 32_768 });
    const messages = conversation(
      taking(1_000, { model: "local" }),
      taking(3_214, { model: "standard" }),
    );

    expect(contextFill(messages, null, local)).toBeNull();
    expect(contextFill(messages, null, model())?.count).toBe(
      "3,214 of 8,192 tokens",
    );
  });

  describe("the share", () => {
    it("is said in whole percent, rounded down", () => {
      expect(said(8_191)?.share).toBe("99% full");
      expect(said(4_096)?.share).toBe("50% full");
    });

    it("says under 1% of a little, and 0% of nothing", () => {
      expect(said(30)).toMatchObject({
        share: "under 1% full",
        percent: (100 * 30) / 8_192,
      });
      expect(said(0)).toMatchObject({ share: "0% full", percent: 0 });
    });

    it("is never more than full, whatever the figure", () => {
      expect(said(8_192)).toMatchObject({ share: "100% full", percent: 100 });
      expect(said(9_000)).toMatchObject({
        share: "100% full",
        percent: 100,
        count: "9,000 of 8,192 tokens",
      });
    });

    it(`says the window is nearly full from ${String(NEARLY_FULL)}% on`, () => {
      expect(said(899, 1_000)).toMatchObject({
        share: "89% full",
        nearlyFull: false,
      });
      expect(said(900, 1_000)).toMatchObject({
        share: "90% full",
        nearlyFull: true,
      });
      expect(said(7_640)).toMatchObject({
        share: "93% full",
        count: "7,640 of 8,192 tokens",
        nearlyFull: true,
      });
    });
  });
});
