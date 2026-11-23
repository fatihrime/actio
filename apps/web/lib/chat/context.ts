// How full the context window of the model that answers is, in this conversation, in
// words. The server says how many tokens of the window a conversation takes after each
// turn (`context_tokens`): what goes back to a model with the next question, and what
// doesn't, is its rule, and the page doesn't copy it. The page takes the newest figure
// there is, divides it by the window, and adds up nothing (ADR 0015, revision "the page
// says how full the model's window is").
import type { Model, Turn } from "@/lib/api/client";
import { shareOf } from "@/lib/usage";

import { WHOLE } from "./figures";
import { turnOf } from "./messages";
import type { ChatMessage } from "./messages";

/** From this share of the window on, the meter says that a new chat is due soon. */
export const NEARLY_FULL = 90;

/** What the meter says of a conversation. */
export interface ContextFill {
  /** How full the window is, for a bar: from 0 to 100. */
  percent: number;
  /** The same in words: "39% full", "under 1% full", never more than "100% full". */
  share: string;
  /** The tokens the conversation takes, of those the window holds: "3,214 of 8,192
   * tokens". */
  count: string;
  /** Whether the share is `NEARLY_FULL` or more. */
  nearlyFull: boolean;
}

/** What the meter says of the conversation `messages`, which `open` stands after when
 * it ends with a question (the turn that last tried to answer it), for `model`, the
 * model that answers. Nothing while no turn has a figure, before the model is known, or
 * when the newest figure is another model's: those are another window's tokens. A turn
 * that is running, was stopped or failed has no figure, and the one before it stands. */
export function contextFill(
  messages: ChatMessage[],
  open: Turn | null,
  model: Model | null,
): ContextFill | null {
  const counted = newestCounted(messages, open);
  if (counted === null || model === null || counted.model !== model.id) {
    return null;
  }
  const { tokens } = counted;
  const window = model.context_window;
  const { percent, label } = shareOf(tokens, window);
  return {
    percent,
    share: `${label} full`,
    count: `${WHOLE.format(tokens)} of ${WHOLE.format(window)} tokens`,
    nearlyFull: percent >= NEARLY_FULL,
  };
}

/** A turn's figure: the tokens its conversation takes, in the window of its model. */
interface Figure {
  model: string;
  tokens: number;
}

/** The newest figure of the conversation: `open`'s when the conversation ends with a
 * question, then each answer's from the newest back. */
function newestCounted(
  messages: ChatMessage[],
  open: Turn | null,
): Figure | null {
  const asked = messages.at(-1)?.role === "user" ? figureOf(open) : null;
  if (asked !== null) {
    return asked;
  }
  for (let index = messages.length - 1; index >= 0; index--) {
    const message = messages[index];
    const figure = message === undefined ? null : figureOf(turnOf(message));
    if (figure !== null) {
      return figure;
    }
  }
  return null;
}

/** A turn's figure, if it has one: a turn that runs, was stopped or failed has none. */
function figureOf(turn: Turn | null): Figure | null {
  return turn !== null && typeof turn.context_tokens === "number"
    ? { model: turn.model, tokens: turn.context_tokens }
    : null;
}
