// What the API streams, read by the library the browser will read it with.
//
// The files in apps/api/tests/fixtures/turns/ are recorded from the API, and a Python
// test fails when the API stops producing exactly them. Here the real `ai` package
// reads the same files, the way `useChat` would: through its transport, which parses
// the events and checks every chunk, and its reader, which builds the message to show.
// So a chunk the library wouldn't accept, or would understand differently, fails here.
import {
  DefaultChatTransport,
  readUIMessageStream,
  safeValidateUIMessages,
} from "ai";
import type { UIMessage } from "ai";
import { describe, expect, it } from "vitest";

import { recorded, RECORDED, streamed } from "../recorded";

/** The part of a message's metadata these tests look at. */
interface Metadata {
  parent_id: string | null;
  versions: string[];
  turn: {
    model: string;
    answered_by: string | null;
    status: string;
    finish_reason: string | null;
    error: string | null;
    input_tokens: number | null;
    output_tokens: number | null;
    context_tokens: number | null;
    generation_ms: number | null;
    credits: number | null;
    capped_by_usage: boolean;
    started_at: string;
    first_word_at: string | null;
    finished_at: string | null;
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
    fetch: () => Promise.resolve(streamed(name)),
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

/** The chunks of a recorded stream, as the library's transport hands them on after
 * checking each against its own description of a chunk. */
async function chunksOf(name: string): Promise<{ type: string }[]> {
  const transport = new DefaultChatTransport<Message>({
    api: "http://actio.test/api/conversations/c1/turns",
    fetch: () => Promise.resolve(streamed(name)),
  });
  const stream = await transport.sendMessages({
    trigger: "submit-message",
    chatId: "c1",
    messageId: undefined,
    messages: [],
    abortSignal: undefined,
  });
  const chunks: { type: string }[] = [];
  const reader = stream.getReader();
  for (;;) {
    const { done, value } = await reader.read();
    if (done) {
      return chunks;
    }
    chunks.push(value);
  }
}

function last(states: Message[]): Message {
  const message = states.at(-1);
  if (message === undefined) {
    throw new Error("the library made no message of the stream");
  }
  return message;
}

const ANSWER_ID = RECORDED.answer;
const QUESTION_ID = RECORDED.question;

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
    expect(first?.turn.context_tokens).toBeNull();
    expect(final?.parent_id).toBe(QUESTION_ID);
    expect(final?.versions).toEqual([ANSWER_ID]);
    expect(final?.turn).toMatchObject({
      status: "completed",
      finish_reason: "stop",
      error: null,
      output_tokens: 6,
      // How much of the window the conversation takes after it: 30 read, 6 written.
      context_tokens: 36,
    });
  });

  it("keeps reasoning apart from the answer", async () => {
    const { states, errors } = await replay("reasoning.sse");

    expect(errors).toEqual([]);
    // What the model thought isn't kept for the next question, nor counted.
    expect(last(states).metadata?.turn).toMatchObject({
      input_tokens: 35,
      output_tokens: 10,
      context_tokens: 40,
    });
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

  it("reads an answer cut at the time its server is given as an answer that ended, and says which limit in the metadata", async () => {
    const { states, errors } = await replay("took_too_long.sse");

    // Not an error: the library has no word for this limit, and says "other".
    expect(errors).toEqual([]);
    expect((await chunksOf("took_too_long.sse")).at(-1)).toMatchObject({
      type: "finish",
      finishReason: "other",
    });
    expect(last(states).parts).toEqual([
      { type: "step-start" },
      { type: "text", text: "One, two, three", state: "done" },
    ]);
    expect(last(states).metadata?.turn).toMatchObject({
      status: "completed",
      finish_reason: "time_limit",
      error: null,
      // A stream that was closed sends no count: what it wrote is counted by guess.
      output_tokens: null,
      context_tokens: null,
    });
    expect(last(states).metadata?.turn.credits).toBeGreaterThan(0);
  });

  it("says in the metadata what the turn cost", async () => {
    const { states } = await replay("answer.sse");

    expect(states[0]?.metadata?.turn).toMatchObject({
      credits: null,
      capped_by_usage: false,
    });
    // The fake model read 30 tokens and wrote 6, at a credit each.
    expect(last(states).metadata?.turn.credits).toBe(36);
  });

  it("carries in the metadata what the page says an answer took", async () => {
    // Recorded with a model that says how long it took to write, and a clock that
    // moved between the question, the first word and the end.
    const { states, errors } = await replay("figures.sse");

    expect(errors).toEqual([]);
    expect(last(states).parts).toEqual([
      { type: "step-start" },
      {
        type: "text",
        text: "Thirty-four tokens, in four seconds.",
        state: "done",
      },
    ]);
    expect(states[0]?.metadata?.turn).toMatchObject({
      status: "running",
      first_word_at: null,
    });
    expect(last(states).metadata?.turn).toMatchObject({
      status: "completed",
      output_tokens: 34,
      generation_ms: 4000,
      started_at: "2026-11-10T12:00:00Z",
      first_word_at: "2026-11-10T12:00:01.200000Z",
      finished_at: "2026-11-10T12:00:03.200000Z",
      credits: 1234,
    });
  });

  it("carries in the metadata which model wrote an answer, when one stood in for the model asked", async () => {
    // The model asked failed before its first word, and the one it names to stand in
    // for it answered, with a window of its own.
    const { states, errors } = await replay("stood_in.sse");

    expect(errors).toEqual([]);
    expect(last(states).parts).toEqual([
      { type: "step-start" },
      { type: "text", text: "From the model that stood in.", state: "done" },
    ]);
    // Nobody had answered when the turn began.
    expect(states[0]?.metadata?.turn).toMatchObject({
      model: "standard",
      answered_by: null,
      status: "running",
    });
    // The turn is the asked model's, and its record names the one that answered, whose
    // figure the window's is: 1,500 tokens read and 36 written.
    expect(last(states).metadata?.turn).toMatchObject({
      model: "standard",
      answered_by: "second",
      status: "completed",
      finish_reason: "stop",
      context_tokens: 1536,
    });
  });

  it("says in the metadata that the model asked wrote its own answer", async () => {
    const { states } = await replay("answer.sse");

    expect(last(states).metadata?.turn).toMatchObject({
      model: "standard",
      answered_by: "standard",
    });
  });

  it("takes a usage warning as a chunk of the stream, and keeps it out of the message", async () => {
    const { states, errors } = await replay("usage_warning.sse");
    const chunks = await chunksOf("usage_warning.sse");

    expect(errors).toEqual([]);
    expect(chunks.filter((chunk) => chunk.type.startsWith("data-"))).toEqual([
      {
        type: "data-usage-warning",
        data: { window: "session", percent: 50 },
        transient: true,
      },
    ]);
    // It is for the page to show once, not something the model said.
    expect(last(states).parts).toEqual([
      { type: "step-start" },
      { type: "text", text: "That was half of it.", state: "done" },
    ]);
  });

  it("reads an answer that was stopped as an answer that ended, and says so in the metadata", async () => {
    const { states, errors } = await replay("stopped.sse");

    // Not an error: the stream ends as a whole answer's does, with a reason of its own.
    expect(errors).toEqual([]);
    expect((await chunksOf("stopped.sse")).at(-1)).toMatchObject({
      type: "finish",
      finishReason: "other",
    });
    expect(last(states).parts).toEqual([
      { type: "step-start" },
      { type: "text", text: "One, two, three", state: "done" },
    ]);
    expect(last(states).metadata?.turn).toMatchObject({
      status: "stopped",
      finish_reason: null,
      error: null,
      output_tokens: null,
    });
    // What it had written is paid for, by the API's guess.
    expect(last(states).metadata?.turn.credits).toBeGreaterThan(0);
  });

  it("skips the line that keeps a silent connection open", async () => {
    // The API sends it while a model says nothing: after `start-step` in this
    // recording, where the model was held.
    expect(recorded("keep_alive.sse")).toContain("\n\n: keep-alive\n\n");

    const { states, errors } = await replay("keep_alive.sse");
    const chunks = await chunksOf("keep_alive.sse");

    expect(errors).toEqual([]);
    expect(chunks.map((chunk) => chunk.type)).toEqual([
      "start",
      "start-step",
      "text-start",
      "text-delta",
      "text-delta",
      "text-end",
      "finish-step",
      "finish",
    ]);
    expect(last(states).parts).toEqual([
      { type: "step-start" },
      { type: "text", text: "Sorry to keep you waiting.", state: "done" },
    ]);
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
      // No model wrote anything.
      answered_by: null,
    });
    expect(message.metadata?.versions).toEqual([]); // no answer was written
  });

  it("reports a stream that is gone as an error, in the API's words, with nothing of the answer", async () => {
    // What someone who reads along is sent when the turn runs on, and its stream can't
    // be read any more. The page reads the conversation instead.
    const { states, errors } = await replay("lost.sse");

    expect(errors).toEqual([
      "The connection to this answer was lost. It may still be being written.",
    ]);
    expect(states.flatMap((state) => state.parts)).toEqual([]);
  });

  it("reports an answer that ended after the server gave up on it as an error, and no ending of its own", async () => {
    // What arrived isn't what the server kept: the page reads the conversation.
    const { states, errors } = await replay("given_up.sse");

    expect(errors).toEqual([
      "This answer was given up on before it ended. The conversation shows what was kept of it.",
    ]);
    expect(
      (await chunksOf("given_up.sse")).map((chunk) => chunk.type),
    ).not.toContain("finish");
    // The metadata is the turn as it started, and says nothing of how it ended.
    expect(last(states).metadata?.turn.status).toBe("running");
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
