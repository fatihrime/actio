import { describe, expect, it } from "vitest";

import type { ChatMessage } from "./messages";
import { turnRequest } from "./turns";

function message(
  id: string,
  role: "user" | "assistant",
  text: string,
): ChatMessage {
  return { id, role, parts: [{ type: "text", text }] };
}

describe("turnRequest", () => {
  it("sends a first question with nothing before it", () => {
    const request = turnRequest({
      messages: [message("q1", "user", "Why is the sky blue?")],
      trigger: "submit-message",
      thinking: false,
    });

    expect(request).toEqual({
      message: {
        id: "q1",
        parts: [{ type: "text", text: "Why is the sky blue?" }],
      },
      parent_id: null,
      thinking: false,
    });
  });

  it("sends a later question as following the answer before it", () => {
    const request = turnRequest({
      messages: [
        message("q1", "user", "Why is the sky blue?"),
        message("a1", "assistant", "Because of scattering."),
        message("q2", "user", "And at sunset?"),
      ],
      trigger: "submit-message",
      thinking: true,
    });

    expect(request).toMatchObject({
      message: { id: "q2" },
      parent_id: "a1",
      thinking: true,
    });
  });

  it("sends only the question's text, never the whole conversation", () => {
    const question: ChatMessage = {
      id: "q1",
      role: "user",
      metadata: {
        parent_id: null,
        created_at: "2026-11-10T12:00:00Z",
        versions: [],
      },
      parts: [
        { type: "text", text: "Hello", state: "done" },
        { type: "reasoning", text: "not the sender's to send" },
      ],
    };

    const request = turnRequest({
      messages: [question],
      trigger: "submit-message",
      thinking: false,
    });

    expect(request.message).toEqual({
      id: "q1",
      parts: [{ type: "text", text: "Hello" }],
    });
    expect(Object.keys(request).sort()).toEqual([
      "message",
      "parent_id",
      "thinking",
    ]);
  });

  it("asks for another answer to a question that is already stored", () => {
    const request = turnRequest({
      messages: [
        message("q1", "user", "Why is the sky blue?"),
        message("a1", "assistant", "Because of scattering."),
        message("q2", "user", "And at sunset?"),
      ],
      trigger: "regenerate-message",
      thinking: false,
    });

    expect(request).toEqual({ answer_to: "q2", thinking: false });
  });

  it("refuses to ask for an answer to an answer", () => {
    expect(() =>
      turnRequest({
        messages: [
          message("q1", "user", "Why is the sky blue?"),
          message("a1", "assistant", "Because of scattering."),
        ],
        trigger: "regenerate-message",
        thinking: false,
      }),
    ).toThrow("Only a question can be answered");
  });
});
