// The web app's calls to Actio's API.
//
// The browser calls the API itself, at the address the page came from: the reverse
// proxy sends /api/* to the API and everything else to this app. The session cookie
// rides along by itself, and no secret ever reaches this code.
//
// The types come from the API's recorded description (./schema.d.ts, generated: see
// `make openapi`), so a call that no longer fits the API fails to compile.
import createClient from "openapi-fetch";
import type { Client } from "openapi-fetch";

import type { components, paths } from "./schema";

type Schemas = components["schemas"];

export type Me = Schemas["Me"];
export type Model = Schemas["ModelOut"];
export type ModelList = Schemas["ModelList"];
export type Conversation = Schemas["ConversationOut"];
export type ConversationSummary = Schemas["ConversationSummary"];
export type ConversationList = Schemas["ConversationList"];
export type StoredMessage = Schemas["MessageOut"];
export type MessageMetadata = Schemas["MessageMetadata"];
export type Turn = Schemas["TurnOut"];
export type TurnRequest = Schemas["TurnRequest"];
export type Problem = Schemas["Problem"];

/** The API refused a request. Carries the problem document it answered with. */
export class ApiProblem extends Error {
  override readonly name = "ApiProblem";
  readonly status: number;
  readonly problem: Problem;

  constructor(problem: Problem) {
    super(problem.detail);
    this.status = problem.status;
    this.problem = problem;
  }

  /** For a 429: how many seconds to wait. */
  get retryAfter(): number | undefined {
    const seconds = this.problem.retry_after;
    return typeof seconds === "number" ? seconds : undefined;
  }

  /** What a refused response says. Every error of the API is a problem document; a
   * proxy in front of it may answer with something else, such as an empty 502. */
  static async from(response: Response): Promise<ApiProblem> {
    let body: unknown;
    try {
      body = await response.json();
    } catch {
      body = undefined;
    }
    return new ApiProblem(asProblem(body, response.status));
  }
}

function asProblem(body: unknown, status: number): Problem {
  if (
    typeof body === "object" &&
    body !== null &&
    "detail" in body &&
    typeof body.detail === "string"
  ) {
    return {
      type: "about:blank",
      title: "",
      ...body,
      detail: body.detail,
      status,
    };
  }
  return {
    type: "about:blank",
    title: "",
    status,
    detail: `The server didn't answer as expected (${String(status)}).`,
  };
}

let client: Client<paths> | undefined;

function api(): Client<paths> {
  // Made on first use, which is always in the browser: only there is the page's own
  // address known. `fetch` is looked up at each call, so a test can replace it.
  client ??= createClient<paths>({
    baseUrl: window.location.origin,
    fetch: (request) => fetch(request),
  });
  return client;
}

/** What a call answered, or the problem it was refused with, thrown. */
function answered<Data>(result: {
  data?: Data;
  error?: unknown;
  response: Response;
}): Data {
  if (!result.response.ok || result.data === undefined) {
    throw new ApiProblem(asProblem(result.error, result.response.status));
  }
  return result.data;
}

/** Whose session this browser has, starting one as a new guest when it has none.
 * Opening the page twice doesn't make two guests: a live session is kept. */
export async function startSession(): Promise<Me> {
  return answered(await api().POST("/api/session/guest"));
}

/** The models on offer, and whether each can answer right now. */
export async function listModels(): Promise<ModelList> {
  return answered(await api().GET("/api/models"));
}

/** A conversation, with the messages of the branch that is shown. */
export async function getConversation(id: string): Promise<Conversation> {
  return answered(
    await api().GET("/api/conversations/{conversation_id}", {
      params: { path: { conversation_id: id } },
    }),
  );
}

/** One page of the caller's conversations, most recently active first. Without a
 * `cursor` it is the first page; each page's `next_cursor` asks for the one after. */
export async function listConversations(
  cursor: string | null = null,
): Promise<ConversationList> {
  return answered(
    await api().GET("/api/conversations", {
      params: { query: cursor === null ? {} : { cursor } },
    }),
  );
}

/** Give a conversation a title. Answers the conversation as it is listed now. */
export async function renameConversation(
  id: string,
  title: string,
): Promise<ConversationSummary> {
  return answered(
    await api().PATCH("/api/conversations/{conversation_id}", {
      params: { path: { conversation_id: id } },
      body: { title },
    }),
  );
}

/** Delete a conversation, with every message in it. There is no way back. */
export async function deleteConversation(id: string): Promise<void> {
  const { error, response } = await api().DELETE(
    "/api/conversations/{conversation_id}",
    { params: { path: { conversation_id: id } } },
  );
  if (!response.ok) {
    throw new ApiProblem(asProblem(error, response.status));
  }
}

/** `fetch` for a streamed answer: a refusal is thrown as the problem it is, instead
 * of being handed on as a response nobody can read. */
export async function fetchStream(
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> {
  const response = await fetch(input, init);
  if (!response.ok) {
    throw await ApiProblem.from(response);
  }
  return response;
}

/** The address a conversation's turns are posted to. */
export function turnsUrl(conversationId: string): string {
  return `/api/conversations/${encodeURIComponent(conversationId)}/turns`;
}
