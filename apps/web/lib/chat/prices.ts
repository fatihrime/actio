// What a model costs, in words: what a row of the menu of models says under its title
// (`ModelPicker`, ADR 0032, revision "the list says what a model costs, and the menu's
// rows say it where the models differ"). The price is the installation's, in credits per
// million tokens, as the list of models says it (ADR 0020): the page divides it by a
// million, and adds up nothing. It is no forecast of what an answer will cost, which is
// said under the answer once it has been counted.
import type { Model, Price } from "@/lib/api/client";

/** How many tokens a price is stated for. */
const MILLION = 1_000_000;

/** A number of credits a token, said exactly: a price is a whole number of millionths
 * of a credit a token, so six decimals at the most, and no zeros after the last digit.
 * Never rounded. */
const EXACTLY = new Intl.NumberFormat("en", { maximumFractionDigits: 6 });

/** What `price` says in words, of what the model writes and what it reads: "Free" when
 * both cost nothing, one number when they cost the same, "1 credit a token", and what
 * it writes first otherwise, "1 credit a token written, 0.25 read". What it reads again
 * from its server's cache isn't said: the list has it. */
export function priceInWords(price: Price): string {
  const { input, output } = price;
  if (input === 0 && output === 0) {
    return "Free";
  }
  const credits = output === MILLION ? "credit" : "credits";
  const written = `${EXACTLY.format(output / MILLION)} ${credits} a token`;
  return input === output
    ? written
    : `${written} written, ${EXACTLY.format(input / MILLION)} read`;
}

/** What each row of the menu says of its model's cost, by the model's id: each its own
 * words, those that are alike too, when the models' words aren't all the same. When they
 * are, none: rows that each say the same tell a reader nothing to choose by. So two
 * models whose prices differ only in what they read again from the cache say none, and
 * neither does one model alone. */
export function pricesSaid(
  models: readonly Model[],
): ReadonlyMap<string, string> {
  const words = new Map(
    models.map((model) => [model.id, priceInWords(model.price)]),
  );
  return new Set(words.values()).size > 1 ? words : new Map();
}
