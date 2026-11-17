// How the page asks the API for an answer: what it posts, and how it reads the reply.
// And how it reads along with an answer that is being written without it.
//
// The chat library (`ai`) would post the whole conversation to a route of its own
// shape. Actio's API keeps the conversation itself, so the page sends only what is
// new: the question, and the message it follows. See ADR 0014.
import { DefaultChatTransport } from "ai";

import { fetchStream, turnStreamUrl, turnsUrl } from "@/lib/api/client";
import type { TurnRequest } from "@/lib/api/client";

import type { ChatMessage } from "./messages";

/** What the library tells a transport about the request it wants made. */
interface Asked {
  /** The messages on the page, ending with the question to answer. */
  messages: ChatMessage[];
  /** `submit-message`: the last message is new. `regenerate-message`: it is already
   * stored, and wants another answer. */
  trigger: "submit-message" | "regenerate-message";
  /** Whether the model should reason before it answers. */
  thinking: boolean;
  /** The model to ask, by its id. Left out: the installation's default one. */
  model?: string;
}

/** The body to post to `/api/conversations/{id}/turns`. */
export function turnRequest({
  messages,
  trigger,
  thinking,
  model,
}: Asked): TurnRequest {
  const question = messages.at(-1);
  if (question?.role !== "user") {
    throw new Error(
      "Only a question can be answered: the last message isn't one.",
    );
  }
  // The default model is asked for by naming none: which one that is, is the API's
  // to say.
  const how = model === undefined ? { thinking } : { thinking, model };
  if (trigger === "regenerate-message") {
    return { answer_to: question.id, ...how };
  }
  return {
    message: {
      id: question.id,
      // Only the text is the sender's to send. The API refuses anything else.
      parts: question.parts.flatMap((part) =>
        part.type === "text" ? [{ type: "text", text: part.text }] : [],
      ),
    },
    // The message before it on the page: the answer it follows, or nothing for a
    // conversation's first question.
    parent_id: messages.at(-2)?.id ?? null,
    ...how,
  };
}

/** What a caller passes along with a message, for `turnRequest` to pick up. */
export interface TurnOptions {
  thinking: boolean;
  model?: string;
}

/** What a caller passes to the library's `resumeStream`, as its `metadata`: the turn
 * to read along with, by its id. */
export interface JoinOptions {
  turn: string;
}

/** The library's connection to one conversation's turns. */
export function turnTransport(
  conversationId: string,
): DefaultChatTransport<ChatMessage> {
  return new DefaultChatTransport<ChatMessage>({
    api: turnsUrl(conversationId),
    fetch: fetchStream,
    prepareSendMessagesRequest: ({ messages, trigger, body }) => {
      const options = body as Partial<TurnOptions> | undefined;
      return {
        body: turnRequest({
          messages,
          trigger,
          thinking: options?.thinking === true,
          model: options?.model,
        }),
      };
    },
    // Joining a stream: the library would ask for one by the conversation alone. The
    // API streams a turn, so the turn is named, and its stream is read from the
    // answer's first event (ADR 0022).
    prepareReconnectToStreamRequest: ({ requestMetadata }) => {
      const turn = (requestMetadata as Partial<JoinOptions> | undefined)?.turn;
      if (typeof turn !== "string") {
        throw new Error(
          "Only a turn can be read along with: no turn was named.",
        );
      }
      return { api: turnStreamUrl(conversationId, turn) };
    },
  });
}
