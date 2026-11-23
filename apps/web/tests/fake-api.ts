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
  Usage,
  UsageWindow,
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
 * session, one model that is ready and can reason, no conversations, and nothing used
 * of the usage limits: what a working installation answers a first visit. A request
 * the test didn't set up fails the test. */
export function fakeApi(): FakeApi {
  const api = new FakeApi()
    .on("POST /api/session/guest", () => json(201, GUEST))
    .on("GET /api/models", () => json(200, models()))
    .on("GET /api/conversations", () => json(200, listed([])))
    .on("GET /api/usage", () => json(200, usage()));
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

/** A refusal the API was recorded giving, such as `limit_reached.json`: the 429 for a
 * question at a usage limit. Its members beyond the usual ones are in no generated
 * type, so a test starts from the real thing. `changes` replaces some of them. */
export function refused(
  name: string,
  changes: Record<string, unknown> = {},
): Response {
  const body = { ...(JSON.parse(recorded(name)) as Problem), ...changes };
  return new Response(JSON.stringify(body), {
    status: body.status,
    headers: { "content-type": "application/problem+json" },
  });
}

/** An answer with nothing in it: 204. For a turn that has ended, there is nothing to
 * read along with. */
export function nothing(): Response {
  return new Response(null, { status: 204 });
}

/** A recorded turn's events, as the API streams them. `answerId` gives the answer
 * another id than the recorded one, for a conversation's second answer. */
export function streamed(name: string, answerId?: string): Response {
  return new Response(events(name, answerId), { headers: STREAM_HEADERS });
}

/** A recorded turn that sends its first `count` events, and then loses its
 * connection: what the browser's `fetch` makes of a network that went away. */
export function brokenAfter(name: string, count: number): Response {
  const first = events(name).split("\n\n").slice(0, count);
  let sent = false;
  const body = new ReadableStream<Uint8Array>({
    // The connection breaks once the events have been read: a stream that errs
    // drops whatever is still waiting to be read.
    pull(controller) {
      if (sent) {
        controller.error(new TypeError("network error"));
        return;
      }
      sent = true;
      controller.enqueue(new TextEncoder().encode(first.join("\n\n") + "\n\n"));
    },
  });
  return new Response(body, { headers: STREAM_HEADERS });
}

/** `answer`, given only once the test calls `release`: a request that takes its
 * time, to see what the page shows meanwhile. */
export function later(answer: Answer): {
  answer: Answer;
  release: () => void;
} {
  let release: () => void = () => undefined;
  const released = new Promise<void>((resolve) => {
    release = resolve;
  });
  return {
    answer: async (call) => {
      await released;
      return answer(call);
    },
    release,
  };
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

/** A recorded turn that the test sends by hand: `send(count)` sends its next `count`
 * events, and `end` the rest of them, after which the stream ends. Nothing arrives
 * meanwhile, and the page reads on: a test ends the turns it began, or stops them. When
 * the page gives up, the stream ends as `heldAfter`'s does. For one request. */
export function sentByHand(name: string): {
  answer: Answer;
  send: (count?: number) => void;
  end: () => void;
} {
  const all = events(name)
    .split("\n\n")
    .filter((event) => event.trim() !== "");
  let controller: ReadableStreamDefaultController<Uint8Array> | undefined;
  const body = new ReadableStream<Uint8Array>({
    start(given) {
      controller = given;
    },
  });
  let sent = 0;
  // Ended, by its last event or by the page giving up: nothing more can be sent.
  let over = false;
  function send(count = 1) {
    if (over) {
      return;
    }
    for (const event of all.slice(sent, sent + count)) {
      controller?.enqueue(new TextEncoder().encode(`${event}\n\n`));
    }
    sent += count;
    if (sent >= all.length) {
      over = true;
      controller?.close();
    }
  }
  return {
    answer: ({ signal }) => {
      signal?.addEventListener("abort", () => {
        if (!over) {
          over = true;
          controller?.error(new DOMException("Aborted", "AbortError"));
        }
      });
      return new Response(body, { headers: STREAM_HEADERS });
    },
    send,
    end: () => {
      send(all.length - sent);
    },
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

/** Someone with a lasting account. `changes` says how they differ: a guest who signs
 * up keeps the guest's id. */
export function account(changes: Partial<Me> = {}): Me {
  return {
    id: "01900000-0000-7000-8000-0000000000e2",
    is_guest: false,
    email: "ada@example.com",
    session_expires_at: "2026-12-10T12:00:00Z",
    ...changes,
  };
}

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

/** A second model on offer, which costs nothing: the one the recorded refusal at a
 * usage limit names (`limit_reached.json`). */
export function freeModel(changes: Partial<Model> = {}): Model {
  return model({
    id: "local",
    title: "Local",
    model: "granite4:micro",
    thinking: false,
    ...changes,
  });
}

/** What `GET /api/usage` answers: a guest's plan, of which nothing is used. `windows`
 * replaces its two windows. */
export function usage(
  windows: UsageWindow[] = [usageWindow("session"), usageWindow("week")],
): Usage {
  return { plan: { id: "guest", title: "Guest" }, windows };
}

/** One window of a plan, with a guest's limit at the repository's settings. `changes`
 * says how it differs: what is used, when it starts over. */
export function usageWindow(
  name: UsageWindow["name"],
  changes: Partial<UsageWindow> = {},
): UsageWindow {
  return {
    name,
    limit: name === "session" ? 25_000 : 100_000,
    used: 0,
    reserved: 0,
    // A session opens with the first question that costs something. The week is the
    // calendar's: this one ends with the Sunday after MOMENT.
    resets_at: name === "session" ? null : "2026-11-16T00:00:00Z",
    ...changes,
  };
}

export function turn(changes: Partial<Turn> = {}): Turn {
  const made: Turn = {
    id: "01900000-0000-7000-8000-000000000001",
    model: "standard",
    thinking: false,
    status: "completed",
    finish_reason: "stop",
    error: null,
    input_tokens: 12,
    cached_input_tokens: 0,
    output_tokens: 6,
    // It thought nothing: the server counts all of it.
    context_tokens: 18,
    load_ms: null,
    prompt_ms: null,
    generation_ms: null,
    // At the repository's prices: a quarter of a credit for a token read, one for a
    // token written.
    credits: 9,
    capped_by_usage: false,
    started_at: MOMENT,
    first_word_at: MOMENT,
    finished_at: MOMENT,
    ...changes,
  };
  // The API says how much of the window a conversation takes only after a turn its
  // model's server counted: one that completed. A test that wants another turn to
  // have a figure says so.
  if (!("context_tokens" in changes) && made.status !== "completed") {
    made.context_tokens = null;
  }
  return made;
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

/** A conversation as the list has it. Its title is on its way only when `changes`
 * says so (`title_pending`). */
export function summary(
  id: string,
  title: string | null,
  changes: Partial<ConversationSummary> = {},
): ConversationSummary {
  return {
    id,
    title,
    title_pending: false,
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
    title_pending: false,
    created_at: MOMENT,
    last_active_at: MOMENT,
    current_leaf_id: messages.at(-1)?.id ?? null,
    messages,
    unanswered,
  };
}
