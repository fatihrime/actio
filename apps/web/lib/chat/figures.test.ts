import { describe, expect, it } from "vitest";

import type { Turn } from "@/lib/api/client";
import { chained, model, turn } from "@/tests/fake-api";

import { figuresOf, SHORTEST_WRITING_MS } from "./figures";

const ASKED = Date.parse("2026-11-10T12:00:00Z");
const SECOND = 1000;
/** The models on offer, as the page has read them: the one the turns here asked. */
const OFFERED = [model()];

/** The moment `ms` milliseconds after the question, as the API writes one. */
function at(ms: number): string {
  return new Date(ASKED + ms).toISOString();
}

/** A turn that has ended, of which nothing is known but what `changes` says: no
 * tokens, no times, no first word, no credits. It was asked at `ASKED`, and ended ten
 * seconds later. */
function ended(changes: Partial<Turn> = {}): Turn {
  return turn({
    input_tokens: null,
    cached_input_tokens: null,
    output_tokens: null,
    credits: null,
    started_at: at(0),
    first_word_at: null,
    finished_at: at(10 * SECOND),
    ...changes,
  });
}

describe("figuresOf", () => {
  it("says how fast, how soon, and what it cost, in that order", () => {
    const took = ended({
      output_tokens: 34,
      generation_ms: 4 * SECOND,
      first_word_at: at(1.2 * SECOND),
      finished_at: at(3.2 * SECOND),
      credits: 1234,
    });

    expect(figuresOf(took, OFFERED)).toEqual([
      "8.5 tokens a second",
      "first word after 1.2 s",
      "1,234 credits",
    ]);
  });

  it("says nothing of a turn that is still being written", () => {
    const writing = ended({
      status: "running",
      output_tokens: 34,
      generation_ms: 4 * SECOND,
      first_word_at: at(SECOND),
      credits: 50,
    });

    expect(figuresOf(writing, OFFERED)).toEqual([]);
  });

  it("says nothing when the record holds nothing", () => {
    expect(figuresOf(ended(), OFFERED)).toEqual([]);
  });

  describe("how fast the model wrote", () => {
    it("is counted over the time the model's server says it took to write", () => {
      // By the clock, 2 seconds from the first word to the end: 17 tokens a second.
      const took = ended({
        output_tokens: 34,
        generation_ms: 4 * SECOND,
        first_word_at: at(SECOND),
        finished_at: at(3 * SECOND),
      });

      expect(figuresOf(took, OFFERED)[0]).toBe("8.5 tokens a second");
    });

    it.each([
      { told: null, how: "no time" },
      { told: 0, how: "a time of nothing" },
    ])(
      "is counted by the clock, from the first word to the end, when the server gives $how",
      ({ told }) => {
        const took = ended({
          output_tokens: 37,
          generation_ms: told,
          first_word_at: at(SECOND),
          finished_at: at(2 * SECOND),
        });

        expect(figuresOf(took, OFFERED)[0]).toBe("37 tokens a second");
      },
    );

    it("is not said over less than a quarter of a second by the clock", () => {
      // A server that sends a whole answer in one piece.
      expect(SHORTEST_WRITING_MS).toBe(SECOND / 4);
      function writtenIn(ms: number): string[] {
        return figuresOf(
          ended({
            output_tokens: 5,
            first_word_at: at(SECOND),
            finished_at: at(SECOND + ms),
          }),
          OFFERED,
        );
      }

      expect(writtenIn(SHORTEST_WRITING_MS - 1)).toEqual([
        "first word after 1.0 s",
      ]);
      expect(writtenIn(SHORTEST_WRITING_MS)).toEqual([
        "20 tokens a second",
        "first word after 1.0 s",
      ]);
    });

    it("is not said over less than a quarter of a second of the server's own time either", () => {
      // Ollama times two tokens over the one gap between them: "Yes." in 26 ms would
      // read 77 tokens a second, from a model that writes 38. The clock, which had
      // two seconds from the first word to the end, is not asked in its place.
      function writtenIn(ms: number, tokens: number): string[] {
        return figuresOf(
          ended({
            output_tokens: tokens,
            generation_ms: ms,
            first_word_at: at(SECOND),
            finished_at: at(3 * SECOND),
          }),
          OFFERED,
        );
      }

      expect(writtenIn(26, 2)).toEqual(["first word after 1.0 s"]);
      expect(writtenIn(SHORTEST_WRITING_MS - 1, 5)).toEqual([
        "first word after 1.0 s",
      ]);
      expect(writtenIn(SHORTEST_WRITING_MS, 5)).toEqual([
        "20 tokens a second",
        "first word after 1.0 s",
      ]);
    });

    it.each([null, 0])("is not said with %s tokens written", (tokens) => {
      const took = ended({
        output_tokens: tokens,
        generation_ms: 4 * SECOND,
        first_word_at: at(SECOND),
        finished_at: at(3 * SECOND),
      });

      expect(figuresOf(took, OFFERED)).toEqual(["first word after 1.0 s"]);
    });

    it("is left out under 0.05 tokens a second, where it would say none of a model that wrote", () => {
      // Over the server's 100 seconds: 4 tokens, and 5.
      const slow = (tokens: number) =>
        figuresOf(
          ended({ output_tokens: tokens, generation_ms: 100 * SECOND }),
          OFFERED,
        );

      expect(slow(4)).toEqual([]);
      expect(slow(5)).toEqual(["0.1 tokens a second"]);
    });

    it.each([
      [700, "7.0 tokens a second"],
      [744, "7.4 tokens a second"],
      [994, "9.9 tokens a second"],
      [996, "10 tokens a second"],
      [1000, "10 tokens a second"],
      [3740, "37 tokens a second"],
      [123_440, "1,234 tokens a second"],
    ])(
      "is said to a tenth, and whole from 10 on: %d tokens in 100 seconds as %s",
      (tokens, words) => {
        const took = ended({
          output_tokens: tokens,
          generation_ms: 100 * SECOND,
        });

        expect(figuresOf(took, OFFERED)).toEqual([words]);
      },
    );
  });

  describe("how long the first word took", () => {
    it("is counted from the question, to the millisecond the API wrote", () => {
      // The API writes moments to the microsecond.
      const took = ended({ first_word_at: "2026-11-10T12:00:00.240567Z" });

      expect(figuresOf(took, OFFERED)).toEqual(["first word after 240 ms"]);
    });

    it("is said when the first word came at the moment of the question", () => {
      expect(figuresOf(ended({ first_word_at: at(0) }), OFFERED)).toEqual([
        "first word after 10 ms",
      ]);
    });

    it.each([
      { when: at(-SECOND), how: "before the question" },
      { when: at(11 * SECOND), how: "after the end" },
      { when: "not a moment", how: "at no moment that can be read" },
    ])("is left out for a first word $how", ({ when }) => {
      // The rate by the clock, which would be counted from it, is left out with it.
      const took = ended({
        output_tokens: 20,
        first_word_at: when,
        credits: 3,
      });

      expect(figuresOf(took, OFFERED)).toEqual(["3 credits"]);
    });
  });

  describe("what it cost", () => {
    it.each([
      [0, "0 credits"],
      [1, "1 credit"],
      [2, "2 credits"],
      [1234, "1,234 credits"],
      [1_234_567, "1,234,567 credits"],
    ])("says %d credits as %s", (credits, words) => {
      expect(figuresOf(ended({ credits }), OFFERED)).toEqual([words]);
    });

    it("is left out when it can't be right", () => {
      expect(figuresOf(ended({ credits: -3 }), OFFERED)).toEqual([]);
    });
  });

  // Since ADR 0028 a model may name others that are asked in its place, and the turn is
  // the asked one's whoever answered: its record names the one that wrote the answer
  // (ADR 0028, revision "a turn says which model answered, and the page says so").
  describe("which model wrote it", () => {
    /** The models of an installation whose default one names a second to stand in for
     * it. */
    const CHAINED = chained().models;
    /** A turn of the default model that the second answered, with a credit or two. */
    const stoodIn = (changes: Partial<Turn> = {}) =>
      ended({ answered_by: "second", credits: 2, ...changes });

    it("is said last, by the title of the model that wrote it in the place of the one asked", () => {
      const took = stoodIn({
        output_tokens: 34,
        generation_ms: 4 * SECOND,
        first_word_at: at(1.2 * SECOND),
        finished_at: at(3.2 * SECOND),
      });

      expect(figuresOf(took, CHAINED)).toEqual([
        "8.5 tokens a second",
        "first word after 1.2 s",
        "2 credits",
        "answered by Second model",
      ]);
      expect(figuresOf(stoodIn({ credits: null }), CHAINED)).toEqual([
        "answered by Second model",
      ]);
    });

    it("is said by the record's name for it, when the models no longer have it", () => {
      expect(figuresOf(stoodIn(), OFFERED)).toEqual([
        "2 credits",
        "answered by second",
      ]);
    });

    it("is not said of an answer the model asked wrote itself, nor of a turn whose record names no model", () => {
      expect(figuresOf(stoodIn({ answered_by: "standard" }), CHAINED)).toEqual([
        "2 credits",
      ]);
      // Recorded before the record named one: taken for the asked model's.
      expect(figuresOf(stoodIn({ answered_by: null }), CHAINED)).toEqual([
        "2 credits",
      ]);
    });

    it("is not said before the models are read, and the rest is said as ever", () => {
      expect(figuresOf(stoodIn(), null)).toEqual(["2 credits"]);
    });

    it("is not said while the answer is still being written", () => {
      expect(figuresOf(stoodIn({ status: "running" }), CHAINED)).toEqual([]);
    });
  });
});
