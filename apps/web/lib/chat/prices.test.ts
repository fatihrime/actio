// What a model costs, in words (lib/chat/prices.ts), and which rows of the menu of
// models say theirs: the rule, apart from the page. What the menu does with it is in
// components/chat/model-picker.test.tsx.
import { describe, expect, it } from "vitest";

import type { Price } from "@/lib/api/client";
import { freeModel, model, secondModel } from "@/tests/fake-api";

import { priceInWords, pricesSaid } from "./prices";

/** A price as the list of models says it, in credits per million tokens. What it reads
 * again from the cache is the price of reading, unless `cached` says otherwise. */
function price(input: number, output: number, cached = input): Price {
  return { input, output, cached_input: cached };
}

describe("priceInWords", () => {
  it.each([
    [0, 0, "Free"],
    [1_000_000, 1_000_000, "1 credit a token"],
    [2_000_000, 2_000_000, "2 credits a token"],
    [500_000, 500_000, "0.5 credits a token"],
    [1, 1, "0.000001 credits a token"],
    [100_000_000, 100_000_000, "100 credits a token"],
    [250_000, 1_000_000, "1 credit a token written, 0.25 read"],
    [750_000, 3_000_000, "3 credits a token written, 0.75 read"],
    [1_000_000, 250_000, "0.25 credits a token written, 1 read"],
    [0, 1_000_000, "1 credit a token written, 0 read"],
    [250_000, 0, "0 credits a token written, 0.25 read"],
  ])("says input %d and output %d as %j", (input, output, said) => {
    expect(priceInWords(price(input, output))).toBe(said);
  });

  it("says each number exactly, never rounded, and with no zeros after its last digit", () => {
    expect(priceInWords(price(25_000, 25_000))).toBe("0.025 credits a token");
    expect(priceInWords(price(1, 1))).toBe("0.000001 credits a token");
    expect(priceInWords(price(2_500_000, 2_500_000))).toBe(
      "2.5 credits a token",
    );
    expect(priceInWords(price(99_999_999, 99_999_999))).toBe(
      "99.999999 credits a token",
    );
    expect(priceInWords(price(1_000_001, 999_999))).toBe(
      "0.999999 credits a token written, 1.000001 read",
    );
    expect(priceInWords(price(1_234_567, 7_654_321))).toBe(
      "7.654321 credits a token written, 1.234567 read",
    );
  });

  it("says one credit, and any other number of credits, by the number said first", () => {
    expect(priceInWords(price(1_000_000, 1_000_000))).toBe("1 credit a token");
    expect(priceInWords(price(1_000_001, 1_000_001))).toBe(
      "1.000001 credits a token",
    );
    expect(priceInWords(price(999_999, 999_999))).toBe(
      "0.999999 credits a token",
    );
    expect(priceInWords(price(500_000, 1_000_000))).toBe(
      "1 credit a token written, 0.5 read",
    );
    expect(priceInWords(price(1_000_000, 2_000_000))).toBe(
      "2 credits a token written, 1 read",
    );
  });

  it("says Free only of a price that is nothing to read and nothing to write", () => {
    expect(priceInWords(price(0, 0))).toBe("Free");
    expect(priceInWords(price(0, 1))).not.toBe("Free");
    expect(priceInWords(price(1, 0, 0))).not.toBe("Free");
  });

  it("says nothing of what the model reads again from its server's cache", () => {
    expect(priceInWords(price(250_000, 1_000_000, 25_000))).toBe(
      "1 credit a token written, 0.25 read",
    );
    expect(priceInWords(price(1_000_000, 1_000_000, 0))).toBe(
      "1 credit a token",
    );
  });
});

describe("pricesSaid", () => {
  it("says none where every model's words are the same", () => {
    const same = [
      model(),
      secondModel(),
      model({ id: "third", title: "Third" }),
    ];

    expect(pricesSaid(same).size).toBe(0);
  });

  it("says every model's own where one differs, those that are alike too", () => {
    const said = pricesSaid([model(), freeModel(), secondModel()]);

    expect(Object.fromEntries(said)).toEqual({
      standard: "1 credit a token written, 0.25 read",
      local: "Free",
      second: "1 credit a token written, 0.25 read",
    });
  });

  it("goes by the words: two prices that differ in what is read again from the cache alone say none", () => {
    const apart = [
      model({ price: price(250_000, 1_000_000, 25_000) }),
      secondModel({ price: price(250_000, 1_000_000, 250_000) }),
    ];

    expect(pricesSaid(apart).size).toBe(0);
  });

  it("says none for one model alone, nor for none", () => {
    expect(pricesSaid([freeModel()]).size).toBe(0);
    expect(pricesSaid([model()]).size).toBe(0);
    expect(pricesSaid([]).size).toBe(0);
  });
});
