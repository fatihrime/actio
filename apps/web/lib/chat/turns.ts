// How the page asks the API for an answer: what it posts, and how it reads the reply.
//
// The chat library (`ai`) would post the whole conversation to a route of its own
// shape. Actio's API keeps the conversation itself, so the page sends only what is
// new: the question, and the message it follows. See ADR 0014.
import { DefaultChatTransport } from "ai";

import { fetchStream, turnsUrl } from "@/lib/api/client";
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
}

/** The body to post to `/api/conversations/{id}/turns`. */
export function turnRequest({
  messages,
  trigger,
  thinking,
}: Asked): TurnRequest {
  const question = messages.at(-1);
  if (question?.role !== "user") {
    throw new Error(
      "Only a question can be answered: the last message isn't one.",
    );
  }
  if (trigger === "regenerate-message") {
    return { answer_to: question.id, thinking };
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
    thinking,
  };
}

/** What a caller passes along with a message, for `turnRequest` to pick up. */
export interface TurnOptions {
  thinking: boolean;
}

/** The library's connection to one conversation's turns. */
export function turnTransport(
  conversationId: string,
): DefaultChatTransport<ChatMessage> {
  return new DefaultChatTransport<ChatMessage>({
    api: turnsUrl(conversationId),
    fetch: fetchStream,
    prepareSendMessagesRequest: ({ messages, trigger, body }) => ({
      body: turnRequest({
        messages,
        trigger,
        thinking: (body as Partial<TurnOptions> | undefined)?.thinking === true,
      }),
    }),
  });
}
