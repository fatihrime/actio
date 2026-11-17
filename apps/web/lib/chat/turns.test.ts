import { describe, expect, it, vi } from "vitest";

import type { ChatMessage } from "./messages";
import { turnRequest, turnTransport } from "./turns";
import type { JoinOptions } from "./turns";

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

  it("names the model only when another than the default one is to answer", () => {
    const messages = [message("q1", "user", "Why is the sky blue?")];

    const asked = turnRequest({
      messages,
      trigger: "submit-message",
      thinking: false,
      model: "local",
    });
    const again = turnRequest({
      messages,
      trigger: "regenerate-message",
      thinking: false,
      model: "local",
    });

    expect(asked).toMatchObject({ message: { id: "q1" }, model: "local" });
    expect(again).toEqual({ answer_to: "q1", thinking: false, model: "local" });
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

describe("turnTransport", () => {
  it("reads along with a turn at that turn's stream", async () => {
    const asked: { url: string; method: string | undefined }[] = [];
    // The library hands `fetch` the address as text.
    vi.stubGlobal("fetch", (url: string, init?: RequestInit) => {
      asked.push({ url, method: init?.method });
      // The turn has ended: there is nothing to read along with.
      return Promise.resolve(new Response(null, { status: 204 }));
    });
    const turn: JoinOptions = { turn: "t?1" };

    const stream = await turnTransport("c 1/2").reconnectToStream({
      chatId: "c 1/2",
      metadata: turn,
    });

    expect(stream).toBeNull();
    // Each id is a single segment of the address, however it is written.
    expect(asked).toEqual([
      { url: "/api/conversations/c%201%2F2/turns/t%3F1/stream", method: "GET" },
    ]);
  });

  it("refuses to read along without being told which turn", async () => {
    const fetching = vi.fn();
    vi.stubGlobal("fetch", fetching);

    await expect(
      turnTransport("c1").reconnectToStream({ chatId: "c1" }),
    ).rejects.toThrow("Only a turn can be read along with");
    expect(fetching).not.toHaveBeenCalled();
  });
});
