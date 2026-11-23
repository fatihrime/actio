// How a turn ended, and what went wrong with a request, in words for a person.
import { ApiProblem } from "@/lib/api/client";
import type { Model, Turn } from "@/lib/api/client";

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
  model_error: "The model's server failed.",
  internal_error: "Something went wrong while the answer was being written.",
};

/** Why a turn left no answer, or not all of one. Nothing when it went well, or is
 * still running. `answered` says whether the conversation has an answer of the turn's
 * with words in it: a turn can complete without one, when its model wrote nothing, or
 * thought and then wrote nothing. The server stores no empty answer, and the turn's
 * record alone doesn't say. */
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
      return "The server was restarted while this answer was being written.";
    case "completed":
      if (turn.finish_reason !== "length") {
        return answered ? null : "The model gave no answer.";
      }
      // Near a usage limit an answer is given less room than the model allows
      // (ADR 0020). When it ran out of that, the limit is why it stops.
      return turn.capped_by_usage
        ? "This answer stops here: your usage limit left no room for more of it."
        : "The answer reached the model's length limit, and stops here.";
    case "running":
      return null;
  }
}

/** Whether a turn left its question open for another try: it ended any other way than
 * completed, or it completed and left no answer with words in it (`answered`, as for
 * `outcomeOf`). */
export function canRetry(turn: Turn, answered: boolean): boolean {
  if (turn.status === "running") {
    return false;
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
