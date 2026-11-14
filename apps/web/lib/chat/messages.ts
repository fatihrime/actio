// A conversation's messages, in the shape the chat library keeps them.
import type { UIMessage } from "ai";

import type { MessageMetadata, StoredMessage, Turn } from "@/lib/api/client";

/** A message as the `ai` library holds it. Everything of Actio's own is under
 * `metadata`: the message it follows, its versions, and for an answer the turn that
 * wrote it. The API stores messages in this shape and streams answers into it. */
export type ChatMessage = UIMessage<MessageMetadata>;

/** Messages as the API stores them, for the library to start from. */
export function fromStored(messages: StoredMessage[]): ChatMessage[] {
  return messages.map((message) => ({
    id: message.id,
    role: message.role,
    metadata: message.metadata,
    parts: message.parts.map((part) => ({ type: part.type, text: part.text })),
  }));
}

/** The text a message says, without its reasoning. */
export function textOf(message: ChatMessage): string {
  return message.parts
    .map((part) => (part.type === "text" ? part.text : ""))
    .join("");
}

/** Whether a message has anything to show yet: some text, or some reasoning. */
export function hasContent(message: ChatMessage): boolean {
  return message.parts.some(
    (part) =>
      (part.type === "text" || part.type === "reasoning") && part.text !== "",
  );
}

/** The turn that wrote an answer, as far as the page knows. */
export function turnOf(message: ChatMessage): Turn | null {
  return message.metadata?.turn ?? null;
}
