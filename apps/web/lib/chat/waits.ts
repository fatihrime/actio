// How many answers an answer waits behind at its model's gate, and that in words. The
// server counts it, by its gate's own rule played forward, and says it in the turn's
// stream each time it changes, and 0 once the answer's turn has come: one part of the
// message, which the chat library replaces in place (`data-waits`, ADR 0023, revision
// "a turn says that it waits, and behind how many answers"). The page reads it from the
// message and from nothing else, and keeps nothing of it: it is a guess at more than
// one answer at a time, it can go up, and it is no time.
import { WHOLE } from "./figures";
import type { ChatMessage } from "./messages";

/** How many answers have to end before `message`'s answer starts, as its stream last
 * said: a whole number from 1 up. 0 says nothing: no such part, its turn has come, or
 * what the part holds isn't a whole number above 0. */
export function waitsBehind(message: ChatMessage): number {
  const [data] = message.parts.flatMap((part) =>
    part.type === "data-waits" ? [part.data] : [],
  );
  if (typeof data !== "object" || data === null || !("behind" in data)) {
    return 0;
  }
  const { behind } = data;
  return typeof behind === "number" && Number.isInteger(behind) && behind > 0
    ? behind
    : 0;
}

/** What the page says of an answer that waits behind `behind` other answers, 1 or more:
 * "Waiting behind 2 other answers…". */
export function waitingBehind(behind: number): string {
  const answers = behind === 1 ? "answer" : "answers";
  return `Waiting behind ${WHOLE.format(behind)} other ${answers}…`;
}
