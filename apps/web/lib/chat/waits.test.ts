import { describe, expect, it } from "vitest";

import type { ChatMessage } from "./messages";
import { waitingBehind, waitsBehind } from "./waits";

/** An answer that has begun, whose stream said `data` of its wait at the gate. */
function answer(...data: unknown[]): ChatMessage {
  return {
    id: "a1",
    role: "assistant",
    parts: [
      { type: "step-start" },
      ...data.map((each) => ({
        type: "data-waits" as const,
        id: "waits",
        data: each,
      })),
    ],
  };
}

describe("waitsBehind", () => {
  it("is the number its stream said, a whole number from 1 up", () => {
    expect(waitsBehind(answer({ behind: 1 }))).toBe(1);
    expect(waitsBehind(answer({ behind: 2 }))).toBe(2);
    expect(waitsBehind(answer({ behind: 1_234 }))).toBe(1_234);
  });

  it("says nothing of an answer whose stream said nothing of a wait", () => {
    expect(waitsBehind(answer())).toBe(0);
    expect(
      waitsBehind({
        id: "a1",
        role: "assistant",
        parts: [{ type: "text", text: "Echo: Hello" }],
      }),
    ).toBe(0);
  });

  it("says nothing once its turn has come", () => {
    expect(waitsBehind(answer({ behind: 0 }))).toBe(0);
  });

  it.each([
    ["a fraction", { behind: 1.5 }],
    ["a negative number", { behind: -2 }],
    ["a number that is none", { behind: Number.NaN }],
    ["no end", { behind: Number.POSITIVE_INFINITY }],
    ["a string", { behind: "2" }],
    ["null", { behind: null }],
    ["a missing member", { ahead: 2 }],
    ["nothing at all", null],
    ["a number alone", 2],
    ["a list", [2]],
  ])("says nothing of %s", (_, data) => {
    expect(waitsBehind(answer(data))).toBe(0);
  });
});

describe("waitingBehind", () => {
  it("says one other answer, and more than one", () => {
    expect(waitingBehind(1)).toBe("Waiting behind 1 other answer…");
    expect(waitingBehind(2)).toBe("Waiting behind 2 other answers…");
    expect(waitingBehind(1_234)).toBe("Waiting behind 1,234 other answers…");
  });
});
