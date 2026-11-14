// A stand-in for Actio's API in component tests: it replaces `fetch`, answers each
// request with what a test set up, and keeps what was asked.
//
// Bodies are typed with the API's generated types, and streamed answers are the
// recorded ones (./recorded.ts), so a test can't run on an answer the API wouldn't give.
import { expect, onTestFinished, vi } from "vitest";

import type {
  Conversation,
  ConversationList,
  ConversationSummary,
  Me,
  Model,
  ModelList,
  Problem,
  StoredMessage,
  Turn,
} from "@/lib/api/client";

import { recorded } from "./recorded";

/** A request the page made. */
export interface Call {
  method: string;
  path: string;
  /** What followed the path's question mark, such as a list's `cursor`. */
  query: Record<string, string>;
  /** The JSON it sent, if any. */
  body: unknown;
  /** Aborted when the page gave up on the request. */
  signal: AbortSignal | undefined;
}

type Answer = (call: Call) => Response | Promise<Response>;

const STREAM_HEADERS = {
  "content-type": "text/event-stream; charset=utf-8",
  "x-vercel-ai-ui-message-stream": "v1",
};

export class FakeApi {
  /** Every request, in the order it was made. */
  readonly calls: Call[] = [];
  private readonly answers = new Map<string, Answer[]>();

  /** Answer requests to `route`, such as `"POST /api/session/guest"`, with `answers`:
   * the first request gets the first, and so on; the last one repeats. In a path,
   * `*` stands for one segment. */
  on(route: string, ...answers: Answer[]): this {
    this.answers.set(route, answers);
    return this;
  }

  /** The requests made to `route`. */
  to(route: string): Call[] {
    return this.calls.filter((call) => matches(route, call));
  }

  readonly fetch = async (
    input: RequestInfo | URL,
    init?: RequestInit,
  ): Promise<Response> => {
    // The API client hands over a whole Request; the chat library an address and
    // what to send there.
    const request = input instanceof Request ? input : null;
    const url = new URL(
      input instanceof Request ? input.url : input,
      window.location.origin,
    );
    const text =
      request !== null
        ? await request.text()
        : typeof init?.body === "string"
          ? init.body
          : "";
    const call: Call = {
      method: (request?.method ?? init?.method ?? "GET").toUpperCase(),
      path: url.pathname,
      query: Object.fromEntries(url.searchParams),
      body: text === "" ? undefined : (JSON.parse(text) as unknown),
      signal: request?.signal ?? init?.signal ?? undefined,
    };
    const route = [...this.answers.keys()].find((key) => matches(key, call));
    const answers = route === undefined ? [] : (this.answers.get(route) ?? []);
    const earlier = route === undefined ? 0 : this.to(route).length;
    const answer = answers[Math.min(earlier, answers.length - 1)];
    if (answer === undefined) {
      this.unexpected.push(`${call.method} ${call.path}`);
      throw new TypeError("fetch failed");
    }
    this.calls.push(call);
    return answer(call);
  };

  /** Requests no answer was set up for: a test that makes one fails. */
  readonly unexpected: string[] = [];
}

function matches(route: string, call: Call): boolean {
  const [method, path] = route.split(" ");
  if (method !== call.method || path === undefined) {
    return false;
  }
  const pattern = path
    .split("/")
    .map((segment) =>
      segment === "*"
        ? "[^/]+"
        : segment.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"),
    );
  return new RegExp(`^${pattern.join("/")}$`).test(call.path);
}

/** Replace `fetch` with a fake API for the running test. It starts with a guest
 * session, one model that is ready and can reason, and no conversations: what a
 * working installation answers a first visit. A request the test didn't set up fails
 * the test. */
export function fakeApi(): FakeApi {
  const api = new FakeApi()
    .on("POST /api/session/guest", () => json(201, GUEST))
    .on("GET /api/models", () => json(200, models()))
    .on("GET /api/conversations", () => json(200, listed([])));
  vi.stubGlobal("fetch", api.fetch);
  onTestFinished(() => {
    expect(api.unexpected, "requests the test set up no answer for").toEqual(
      [],
    );
  });
  return api;
}

// --- Answers ----------------------------------------------------------------------

export function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** A refusal, as the API words it. */
export function problem(
  status: number,
  detail: string,
  extra: Record<string, unknown> = {},
): Response {
  const body: Problem = {
    type: "about:blank",
    title: "",
    status,
    detail,
    ...extra,
  };
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/problem+json" },
  });
}

/** A recorded turn's events, as the API streams them. `answerId` gives the answer
 * another id than the recorded one, for a conversation's second answer. */
export function streamed(name: string, answerId?: string): Response {
  return new Response(events(name, answerId), { headers: STREAM_HEADERS });
}

/** A recorded turn that sends its first `count` events, and then nothing more until
 * the page gives up: an answer caught half-way. */
export function heldAfter(name: string, count: number): Answer {
  return ({ signal }) => {
    const first = events(name).split("\n\n").slice(0, count);
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(
          new TextEncoder().encode(first.join("\n\n") + "\n\n"),
        );
        signal?.addEventListener("abort", () => {
          controller.error(new DOMException("Aborted", "AbortError"));
        });
      },
    });
    return new Response(body, { headers: STREAM_HEADERS });
  };
}

function events(name: string, answerId?: string): string {
  const text = recorded(name);
  return answerId === undefined
    ? text
    : text.replaceAll("01900000-0000-7000-8000-000000000002", answerId);
}

// --- What the API holds -----------------------------------------------------------

const MOMENT = "2026-11-10T12:00:00Z";

export const GUEST: Me = {
  id: "01900000-0000-7000-8000-0000000000e1",
  is_guest: true,
  email: null,
  session_expires_at: "2026-11-17T12:00:00Z",
};

export function model(changes: Partial<Model> = {}): Model {
  return {
    id: "standard",
    title: "Qwen 3.5 4B",
    provider: "ollama",
    model: "qwen3.5:4b-q4_K_M",
    context_window: 8192,
    max_output_tokens: 2048,
    status: "ready",
    thinking: true,
    ...changes,
  };
}

export function models(changes: Partial<Model> = {}): ModelList {
  return { default: "standard", models: [model(changes)] };
}

export function turn(changes: Partial<Turn> = {}): Turn {
  return {
    id: "01900000-0000-7000-8000-000000000001",
    model: "standard",
    thinking: false,
    status: "completed",
    finish_reason: "stop",
    error: null,
    input_tokens: 12,
    cached_input_tokens: 0,
    output_tokens: 6,
    load_ms: null,
    prompt_ms: null,
    generation_ms: null,
    started_at: MOMENT,
    finished_at: MOMENT,
    ...changes,
  };
}

export function question(
  id: string,
  text: string,
  parentId: string | null = null,
): StoredMessage {
  return {
    id,
    role: "user",
    parts: [{ type: "text", text }],
    metadata: { parent_id: parentId, created_at: MOMENT, versions: [id] },
  };
}

export function answer(
  id: string,
  text: string,
  parentId: string,
  by: Turn = turn(),
): StoredMessage {
  return {
    id,
    role: "assistant",
    parts: [{ type: "text", text }],
    metadata: {
      parent_id: parentId,
      created_at: MOMENT,
      versions: [id],
      turn: by,
    },
  };
}

/** A conversation as the list has it. */
export function summary(
  id: string,
  title: string | null,
  changes: Partial<ConversationSummary> = {},
): ConversationSummary {
  return {
    id,
    title,
    created_at: MOMENT,
    last_active_at: MOMENT,
    ...changes,
  };
}

/** One page of the list. `nextCursor` says that there is a page after it. */
export function listed(
  items: ConversationSummary[],
  nextCursor: string | null = null,
): ConversationList {
  return { items, next_cursor: nextCursor };
}

/** A stored conversation. `unanswered` is for one that ends with a question. */
export function conversation(
  id: string,
  messages: StoredMessage[],
  unanswered: Turn | null = null,
): Conversation {
  return {
    id,
    title: null,
    created_at: MOMENT,
    last_active_at: MOMENT,
    current_leaf_id: messages.at(-1)?.id ?? null,
    messages,
    unanswered,
  };
}
