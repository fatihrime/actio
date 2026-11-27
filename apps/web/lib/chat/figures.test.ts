import { describe, expect, it } from "vitest";

import type { Turn } from "@/lib/api/client";
import { chained, model, secondModel, turn } from "@/tests/fake-api";

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
  // (ADR 0028, revision "a turn says which model answered, and the page says so"). And
  // since ADR 0032 the reader chooses the model that answers, so that two answers of one
  // conversation can be two models' own: the line names the model that wrote each,
  // wherever a reader could take it for another's (ADR 0032, revision "an answer says
  // which model wrote it, where there is a choice").
  describe("which model wrote it", () => {
    /** The models of an installation whose default one names a second to stand in for
     * it: "Qwen 3.5 4B" and "Second model". */
    const CHAINED = chained().models;
    /** A turn that `asked` was asked, and whose record says `by` wrote its answer, with
     * a credit or two. */
    const answered = (
      asked: string,
      by: string | null,
      changes: Partial<Turn> = {},
    ) => ended({ model: asked, answered_by: by, credits: 2, ...changes });

    it.each([
      {
        asked: "standard",
        by: "standard",
        offered: "it alone",
        models: OFFERED,
        said: [],
      },
      {
        asked: "standard",
        by: "standard",
        offered: "it and another",
        models: CHAINED,
        said: ["answered by Qwen 3.5 4B"],
      },
      {
        asked: "local",
        by: "local",
        offered: "another alone",
        models: OFFERED,
        said: ["answered by local"],
      },
      {
        asked: "standard",
        by: "second",
        offered: "both",
        models: CHAINED,
        said: ["answered by Second model in place of Qwen 3.5 4B"],
      },
      {
        asked: "standard",
        by: "second",
        offered: "the one asked alone",
        models: OFFERED,
        said: ["answered by second in place of Qwen 3.5 4B"],
      },
      {
        asked: "gone",
        by: "standard",
        offered: "the one that wrote it alone",
        models: OFFERED,
        said: ["answered by Qwen 3.5 4B in place of gone"],
      },
    ])(
      "asked of $asked and written by $by, with $offered on offer, says $said",
      ({ asked, by, models, said }) => {
        expect(figuresOf(answered(asked, by), models)).toEqual([
          "2 credits",
          ...said,
        ]);
      },
    );

    it("is said last, after every other figure, and alone when the record has no other", () => {
      const took = {
        output_tokens: 34,
        generation_ms: 4 * SECOND,
        first_word_at: at(1.2 * SECOND),
        finished_at: at(3.2 * SECOND),
      };
      const figures = ["8.5 tokens a second", "first word after 1.2 s"];

      expect(figuresOf(answered("standard", "second", took), CHAINED)).toEqual([
        ...figures,
        "2 credits",
        "answered by Second model in place of Qwen 3.5 4B",
      ]);
      expect(
        figuresOf(answered("standard", "standard", took), CHAINED),
      ).toEqual([...figures, "2 credits", "answered by Qwen 3.5 4B"]);
      expect(
        figuresOf(answered("standard", "second", { credits: null }), CHAINED),
      ).toEqual(["answered by Second model in place of Qwen 3.5 4B"]);
      expect(
        figuresOf(answered("standard", "standard", { credits: null }), CHAINED),
      ).toEqual(["answered by Qwen 3.5 4B"]);
    });

    it.each([
      { offered: "one model", models: OFFERED },
      { offered: "two models", models: CHAINED },
      { offered: "no model it names", models: [secondModel()] },
    ])(
      "is not said of a turn whose record names no model that wrote it, with $offered on offer",
      ({ models }) => {
        // Given up on, it may hold a stand-in's draft; or recorded before the record
        // named one. Either way the record doesn't say it was the asked model's.
        expect(figuresOf(answered("standard", null), models)).toEqual([
          "2 credits",
        ]);
      },
    );

    it("is not said before the models are read, and the rest is said as ever", () => {
      expect(figuresOf(answered("standard", "second"), null)).toEqual([
        "2 credits",
      ]);
      expect(figuresOf(answered("standard", "standard"), null)).toEqual([
        "2 credits",
      ]);
    });

    it("is not said while the answer is still being written", () => {
      expect(
        figuresOf(
          answered("standard", "second", { status: "running" }),
          CHAINED,
        ),
      ).toEqual([]);
      expect(
        figuresOf(
          answered("standard", "standard", { status: "running" }),
          CHAINED,
        ),
      ).toEqual([]);
    });
  });
});
