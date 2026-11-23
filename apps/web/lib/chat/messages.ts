// A conversation's messages, in the shape the chat library keeps them.
import type { UIMessage } from "ai";

import type { MessageMetadata, StoredMessage, Turn } from "@/lib/api/client";

/** What an answer's stream carries beside the answer: said once to the page, and never
 * part of a message. The API writes these chunks in `actio.api.ui_stream`. */
export interface ChatData extends Record<string, unknown> {
  /** A share of a usage limit was passed by this turn. What it says is checked when
   * it arrives (`passedIn` in `lib/usage.ts`). */
  "usage-warning": unknown;
}

/** A message as the `ai` library holds it. Everything of Actio's own is under
 * `metadata`: the message it follows, its versions, and for an answer the turn that
 * wrote it. The API stores messages in this shape and streams answers into it. */
export type ChatMessage = UIMessage<MessageMetadata, ChatData>;

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

/** Whether a message says anything: some text that isn't blank. What a model thought
 * before it answered is no answer. */
export function hasWords(message: ChatMessage): boolean {
  return textOf(message).trim() !== "";
}

/** Whether a message has nothing in it to read: no text and no reasoning but blank
 * space. A model can write that, a line break and no more, and the server stores it. */
export function isBlank(message: ChatMessage): boolean {
  return !message.parts.some(
    (part) =>
      (part.type === "text" || part.type === "reasoning") &&
      part.text.trim() !== "",
  );
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
