// What the API streams, read by the library the browser will read it with.
//
// The files in apps/api/tests/fixtures/turns/ are recorded from the API, and a Python
// test fails when the API stops producing exactly them. Here the real `ai` package
// reads the same files, the way `useChat` would: through its transport, which parses
// the events and checks every chunk, and its reader, which builds the message to show.
// So a chunk the library wouldn't accept, or would understand differently, fails here.
import { readFileSync } from "node:fs";

import {
  DefaultChatTransport,
  readUIMessageStream,
  safeValidateUIMessages,
} from "ai";
import type { UIMessage } from "ai";
import { describe, expect, it } from "vitest";

const fixtures = new URL("../../../api/tests/fixtures/turns/", import.meta.url);

function recorded(name: string): string {
  return readFileSync(new URL(name, fixtures), "utf8");
}

/** The part of a message's metadata these tests look at. */
interface Metadata {
  parent_id: string | null;
  versions: string[];
  turn: {
    status: string;
    finish_reason: string | null;
    error: string | null;
    output_tokens: number | null;
  };
}

type Message = UIMessage<Metadata>;

/** Replays a recorded stream through the library. Returns the message as it stood
 * after each chunk, and the errors the library reported on the way. */
async function replay(
  name: string,
): Promise<{ states: Message[]; errors: string[] }> {
  const transport = new DefaultChatTransport<Message>({
    api: "http://actio.test/api/conversations/c1/turns",
    fetch: () =>
      Promise.resolve(
        new Response(recorded(name), {
          headers: {
            "content-type": "text/event-stream; charset=utf-8",
            "x-vercel-ai-ui-message-stream": "v1",
          },
        }),
      ),
  });
  const stream = await transport.sendMessages({
    trigger: "submit-message",
    chatId: "c1",
    messageId: undefined,
    messages: [],
    abortSignal: undefined,
  });
  const states: Message[] = [];
  const errors: string[] = [];
  const reading = readUIMessageStream<Message>({
    stream,
    onError: (error) =>
      errors.push(error instanceof Error ? error.message : String(error)),
  });
  for await (const message of reading) {
    states.push(structuredClone(message));
  }
  return { states, errors };
}

function last(states: Message[]): Message {
  const message = states.at(-1);
  if (message === undefined) {
    throw new Error("the library made no message of the stream");
  }
  return message;
}

const ANSWER_ID = "01900000-0000-7000-8000-000000000002";
const QUESTION_ID = "01900000-0000-7000-8000-0000000000a1";

describe("a recorded turn, read by the ai package", () => {
  it("becomes the assistant's message, growing piece by piece", async () => {
    const { states, errors } = await replay("answer.sse");

    expect(errors).toEqual([]);
    const message = last(states);
    expect(message.id).toBe(ANSWER_ID);
    expect(message.role).toBe("assistant");
    expect(message.parts).toEqual([
      { type: "step-start" },
      { type: "text", text: "Echo: Why is the sky blue?", state: "done" },
    ]);
    // The text grew while the answer was written: more than one state was shown.
    const lengths = states.map((state) =>
      state.parts.reduce(
        (sum, part) => sum + (part.type === "text" ? part.text.length : 0),
        0,
      ),
    );
    expect(new Set(lengths).size).toBeGreaterThan(3);
    expect(lengths).toEqual([...lengths].sort((a, b) => a - b));
  });

  it("carries what the API says about the turn as the message's metadata", async () => {
    const { states } = await replay("answer.sse");

    const first = states[0]?.metadata;
    const final = last(states).metadata;
    expect(first?.turn.status).toBe("running");
    expect(final?.parent_id).toBe(QUESTION_ID);
    expect(final?.versions).toEqual([ANSWER_ID]);
    expect(final?.turn).toMatchObject({
      status: "completed",
      finish_reason: "stop",
      error: null,
      output_tokens: 6,
    });
  });

  it("keeps reasoning apart from the answer", async () => {
    const { states, errors } = await replay("reasoning.sse");

    expect(errors).toEqual([]);
    expect(last(states).parts).toEqual([
      { type: "step-start" },
      // The library keeps the id the API gave the reasoning part.
      {
        type: "reasoning",
        id: "reasoning-0",
        text: "Blue light scatters most.",
        state: "done",
      },
      { type: "text", text: "Echo: Why is the sky blue?", state: "done" },
    ]);
  });

  it("says in the metadata when an answer was cut off at the limit", async () => {
    const { states, errors } = await replay("cut_off.sse");

    expect(errors).toEqual([]);
    expect(last(states).parts.at(-1)).toEqual({
      type: "text",
      text: "One, two, three",
      state: "done",
    });
    expect(last(states).metadata?.turn).toMatchObject({
      status: "completed",
      finish_reason: "length",
    });
  });

  it("reports a model that can't be reached as an error, with the turn's outcome", async () => {
    const { states, errors } = await replay("model_unavailable.sse");

    expect(errors).toEqual([
      "The model's server can't be reached, or is too busy. Try again in a moment.",
    ]);
    const message = last(states);
    expect(message.parts.filter((part) => part.type === "text")).toEqual([]);
    expect(message.metadata?.turn).toMatchObject({
      status: "failed",
      error: "model_unavailable",
    });
    expect(message.metadata?.versions).toEqual([]); // no answer was written
  });

  it("keeps the part of an answer that arrived before it broke off", async () => {
    const { states, errors } = await replay("broke_off.sse");

    expect(errors).toEqual([
      "The answer broke off: the connection to the model was lost.",
    ]);
    const message = last(states);
    expect(message.parts.at(-1)).toMatchObject({
      type: "text",
      text: "Echo: one two ",
    });
    expect(message.metadata?.turn).toMatchObject({
      status: "failed",
      error: "answer_interrupted",
    });
    expect(message.metadata?.versions).toEqual([ANSWER_ID]); // the half answer was kept
  });
});

describe("a recorded conversation, read by the ai package", () => {
  it("is a list of the library's own kind of message", async () => {
    const conversation = JSON.parse(recorded("conversation.json")) as {
      messages: unknown;
    };

    const checked = await safeValidateUIMessages<Message>({
      messages: conversation.messages,
    });

    expect(checked.success).toBe(true);
    if (!checked.success) {
      return;
    }
    expect(checked.data.map((message) => message.role)).toEqual([
      "user",
      "assistant",
    ]);
    expect(checked.data[1]?.parts).toEqual([
      { type: "reasoning", text: "Blue light scatters most." },
      { type: "text", text: "Echo: Why is the sky blue?" },
    ]);
    expect(checked.data[1]?.id).toBe(ANSWER_ID);
  });
});
