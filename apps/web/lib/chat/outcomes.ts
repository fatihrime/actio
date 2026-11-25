// How a turn ended, and what went wrong with a request, in words for a person.
import { ApiProblem } from "@/lib/api/client";
import type { Model, Turn } from "@/lib/api/client";

import { answeredInPlace } from "./figures";

/** What the API stores for a failed turn is a code (`TurnOut.error`). These are the
 * words for each. The API has its own, which it sends while streaming; the page
 * shows these, so a turn reads the same live and after a reload. */
const FAILURES: Record<string, string> = {
  model_unavailable:
    "The model's server can't be reached, or is too busy. Try again in a moment.",
  model_missing: "The model isn't on its server.",
  context_overflow:
    "This conversation has become too long for the model. Start a new one, or ask again in fewer words.",
  model_refused: "The model's server refused the request.",
  answer_interrupted:
    "The answer broke off: the connection to the model was lost.",
  model_too_slow: "The model took too long, and wrote no answer.",
  model_error: "The model's server failed.",
  internal_error: "Something went wrong while the answer was being written.",
};

/** Why a turn left no answer, or not all of one. Nothing when it went well, or is
 * still running. `answered` says whether the conversation has an answer of the turn's
 * with words in it: a turn can complete without one, when its model wrote nothing, or
 * thought and then wrote nothing. The server stores no empty answer, and the turn's
 * record alone doesn't say.
 *
 * Three limits can end an answer, and the server says which (ADR 0014, revision "a turn
 * says which limit its answer reached"): the end of the model's context window, which
 * the conversation and the answer filled together (`context_window`), the longest a
 * single answer may be (`length`), or the longest it may take, which its model's server
 * is given (`time_limit`, ADR 0028, revision "the longest an answer may take").
 *
 * The window that was filled is the one of the model that answered. When that was a
 * model asked in the place of the one asked, the page says so, and another try is
 * offered: the model asked may answer it (ADR 0028, revision "a turn says which model
 * answered, and the page says so"). */
export function outcomeOf(turn: Turn, answered: boolean): string | null {
  switch (turn.status) {
    case "failed":
      return (
        FAILURES[turn.error ?? ""] ??
        "Something went wrong while the answer was being written."
      );
    case "stopped":
      return "This answer was stopped before it was finished.";
    case "interrupted":
      // A shutdown, a process that died, or one that was silent for so long that the
      // server gave up on its turn (ADR 0022).
      return "This answer broke off: the server that was writing it was restarted, or stopped answering.";
    case "completed":
      return completedWith(turn, answered);
    case "running":
      return null;
  }
}

/** What is said of a turn that completed. */
function completedWith(turn: Turn, answered: boolean): string | null {
  if (turn.finish_reason === "context_window" && answeredInPlace(turn)) {
    return answered
      ? "Another model answered in place of the one you asked, and the conversation has filled its context window: the answer stops here. Try again, or start a new chat to go on."
      : "Another model answered in place of the one you asked, and the conversation has filled its context window: it wrote no answer. Try again, or start a new chat to go on.";
  }
  if (turn.finish_reason === "context_window") {
    return answered
      ? "The conversation has filled the model's context window, and the answer stops here. Start a new chat to go on."
      : "The conversation has filled the model's context window, and the model wrote no answer. Start a new chat to go on.";
  }
  if (turn.finish_reason === "time_limit") {
    if (answered) {
      return "The answer took the longest a single answer may take, and stops here.";
    }
    // Its reasoning counts toward the same time, and can use it all up.
    return turn.thinking
      ? "The model was still thinking after the longest a single answer may take, and wrote no answer. Try again, or turn off “Think before answering” first."
      : "The model took the longest a single answer may take, and wrote no answer.";
  }
  if (turn.finish_reason !== "length") {
    return answered ? null : "The model gave no answer.";
  }
  // Near a usage limit an answer is given less room than the model allows (ADR 0020).
  // When it ran out of that, the limit is why it stops.
  if (turn.capped_by_usage) {
    return "This answer stops here: your usage limit left no room for more of it.";
  }
  if (answered) {
    return "The answer reached the longest a single answer may be, and stops here.";
  }
  // Its reasoning counts toward the same limit, and can use it all up.
  return turn.thinking
    ? "The model was still thinking when it reached the longest a single answer may be, and wrote no answer. Try again, or turn off “Think before answering” first."
    : "The model reached the longest a single answer may be, and wrote no answer.";
}

/** Whether a turn left its question open for another try: it ended any other way than
 * completed, or it completed and left no answer with words in it (`answered`, as for
 * `outcomeOf`). Not at the end of the context window: another try of the same
 * question meets it again. But for the window of a model asked in the place of the
 * one asked: the one asked may answer another try. */
export function canRetry(turn: Turn, answered: boolean): boolean {
  if (turn.status === "running") {
    return false;
  }
  if (turn.status === "completed" && turn.finish_reason === "context_window") {
    return answeredInPlace(turn);
  }
  return turn.status !== "completed" || !answered;
}

/** What went wrong with a request, as far as the page can tell. */
export function explain(error: unknown): string {
  if (error instanceof ApiProblem) {
    if (error.status === 401) {
      return "Your session has ended.";
    }
    // The API writes `detail` for people.
    return error.message;
  }
  return "Actio's server can't be reached. Check that it is running, and your connection.";
}

/** Whether an error means the session is gone, so nothing will work until a new one
 * is started. */
export function isSessionEnd(error: unknown): boolean {
  return error instanceof ApiProblem && error.status === 401;
}

/** Why a model can't answer right now, and what to do about it. Nothing when it can. */
export function notReady(
  model: Model,
): { title: string; advice: string; command: string | null } | null {
  if (model.status === "ready") {
    return null;
  }
  const local = model.provider === "ollama";
  if (model.status === "missing") {
    return {
      title: `${model.title} isn't downloaded yet.`,
      advice: local
        ? "On the machine that runs Actio, download it:"
        : `Its server doesn't offer a model called ${model.model}.`,
      command: local ? `ollama pull ${model.model}` : null,
    };
  }
  return {
    title: `${model.title} can't be reached.`,
    advice: local
      ? "Start Ollama on the machine that runs Actio. This page notices when it answers."
      : "Its server doesn't answer. This page notices when it does.",
    command: null,
  };
}
