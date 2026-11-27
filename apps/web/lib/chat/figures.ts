// What an answer took, in words: how fast its model wrote, how long its first word
// took, what it cost, and which model wrote it, where a reader could take it for
// another's. The server counts the tokens and the credits, notes the moments and names
// the model; the page only divides, and words the numbers (ADR 0015, revision "an answer
// says what it took"; ADR 0028, revision "a turn says which model answered, and the page
// says so"; ADR 0032, revision "an answer says which model wrote it, where there is a
// choice").
import type { Model, Turn } from "@/lib/api/client";
import { timeTaken } from "@/lib/time";

/** The shortest time of writing over which a rate is said, in milliseconds, whoever
 * timed it. By the clock, a server that sends a whole answer in one piece would
 * otherwise be said to write thousands of tokens a second. And a server's own time is
 * no better over a few tokens: Ollama times the tokens it wrote from the first to the
 * last, so two tokens are timed over one gap, and a model that writes 38 a second is
 * said to have written 77. */
export const SHORTEST_WRITING_MS = 250;

/** Whole numbers, grouped as English groups them: "1,234". */
export const WHOLE = new Intl.NumberFormat("en", { maximumFractionDigits: 0 });

/** The model whose answer a turn holds, and whose its figures are: the one its record
 * names (`answered_by`), the model asked or one asked in its place (ADR 0028). A turn
 * recorded before the record named one is taken for the asked model's, `model`. The
 * tokens a turn counted are that model's, in that model's own context window. */
export function answeredBy(turn: Turn): string {
  return turn.answered_by ?? turn.model;
}

/** Whether a model asked in the place of the one the turn asked wrote its answer. Never
 * for a turn whose record names no model that answered. */
export function answeredInPlace(turn: Turn): boolean {
  return answeredBy(turn) !== turn.model;
}

/** What a turn took, as the words to show under its answer, in this order: how fast
 * its model wrote, how long its first word took, what it cost, and, last, which model
 * wrote it (`writtenBy`). That one waits for `models`, the models on offer, `null`
 * until they are read: last, so that nothing else in the list moves when it comes. Only
 * the figures its record holds and that can be right; none while it is still being
 * written. */
export function figuresOf(
  turn: Turn,
  models: readonly Model[] | null,
): string[] {
  if (turn.status === "running") {
    return [];
  }
  const figures: string[] = [];
  const rate = rateOf(turn);
  const tenths = rate === null ? 0 : Math.round(rate * 10) / 10;
  // A rate under 0.05 would say "0.0" of a model that wrote: that is no figure.
  if (rate !== null && tenths > 0) {
    const said = tenths < 10 ? tenths.toFixed(1) : WHOLE.format(rate);
    figures.push(`${said} tokens a second`);
  }
  const firstWord = firstWordOf(turn);
  if (firstWord !== null) {
    figures.push(`first word after ${timeTaken(firstWord)}`);
  }
  const { credits } = turn;
  if (credits !== null && credits >= 0) {
    figures.push(
      `${WHOLE.format(credits)} ${credits === 1 ? "credit" : "credits"}`,
    );
  }
  const writer = models === null ? null : writtenBy(turn, models);
  if (writer !== null) {
    figures.push(writer);
  }
  return figures;
}

/** Which model wrote a turn's answer, in words, wherever a reader could take it for
 * another's: "answered by" the model that wrote it, and "in place of" the one asked
 * when that wasn't the one that wrote it, each by its title among `models`, or by its
 * name in the record when they no longer have it. Not said of the asked model's own
 * answer where it is the one model on offer: there it would say the same under each
 * such answer, what the empty conversation says already. Nor of a turn whose record
 * names no model that wrote it, however many are on offer: one given up on may hold a
 * stand-in's words, so such a turn isn't taken for the asked model's here, as
 * `answeredBy` takes it. */
function writtenBy(turn: Turn, models: readonly Model[]): string | null {
  const writer = turn.answered_by;
  if (writer === null) {
    return null;
  }
  const titled = (id: string) =>
    models.find((model) => model.id === id)?.title ?? id;
  if (writer !== turn.model) {
    return `answered by ${titled(writer)} in place of ${titled(turn.model)}`;
  }
  const [only, ...others] = models;
  return others.length === 0 && only?.id === writer
    ? null
    : `answered by ${titled(writer)}`;
}

/** How many tokens a second the model wrote: over the time its server says it took to
 * write, or else over Actio's clock, from the first word to the end. None over less
 * than `SHORTEST_WRITING_MS`. */
function rateOf(turn: Turn): number | null {
  const tokens = turn.output_tokens;
  if (tokens === null || tokens <= 0) {
    return null;
  }
  const ms = writingOf(turn);
  if (ms === null || ms < SHORTEST_WRITING_MS) {
    return null;
  }
  return (tokens * 1000) / ms;
}

/** How long the model took to write, in milliseconds, if that can be told: by its
 * server's own time, or else by Actio's clock, from the first word to the end. */
function writingOf(turn: Turn): number | null {
  if (turn.generation_ms !== null && turn.generation_ms > 0) {
    return turn.generation_ms;
  }
  const first = firstWordAt(turn);
  const end = moment(turn.finished_at);
  return first === null || end === null ? null : end - first;
}

/** How long the first word took, in milliseconds: from the question to the first piece
 * of the answer. */
function firstWordOf(turn: Turn): number | null {
  const first = firstWordAt(turn);
  const asked = moment(turn.started_at);
  return first === null || asked === null ? null : first - asked;
}

/** When the first word came, where the record can place it: not before the question,
 * and not after the end. A moment that can't be right is taken for none, and so is
 * whatever would be counted from it. */
function firstWordAt(turn: Turn): number | null {
  const first = moment(turn.first_word_at);
  const asked = moment(turn.started_at);
  const end = moment(turn.finished_at);
  if (first === null || asked === null || first < asked) {
    return null;
  }
  return end !== null && first > end ? null : first;
}

/** A moment the API wrote, in milliseconds since 1970: nothing when there is none, or
 * none that can be read. */
function moment(written: string | null): number | null {
  if (written === null) {
    return null;
  }
  const ms = Date.parse(written);
  return Number.isFinite(ms) ? ms : null;
}
