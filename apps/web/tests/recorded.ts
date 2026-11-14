// What the API really sends, as files the API's own tests keep true.
//
// - `apps/api/tests/fixtures/turns/`: streamed answers and a stored conversation. A
//   Python test fails when the API stops producing exactly these files.
// - `apps/api/openapi.json`: the API's description of itself.
//
// The web app's tests read them, so they never run on a guess of what the API says.
import { readFileSync } from "node:fs";
import path from "node:path";

const api = path.join(import.meta.dirname, "../../api");

/** A recorded turn, such as `answer.sse`, or the recorded `conversation.json`. */
export function recorded(name: string): string {
  return readFileSync(path.join(api, "tests/fixtures/turns", name), "utf8");
}

/** Where the API's recorded description is. */
export const API_DESCRIPTION = path.join(api, "openapi.json");

/** A streamed answer, as `fetch` would give it: the events of a recorded turn. */
export function streamed(name: string): Response {
  return new Response(recorded(name), {
    headers: {
      "content-type": "text/event-stream; charset=utf-8",
      "x-vercel-ai-ui-message-stream": "v1",
    },
  });
}

/** The ids in the recorded files. */
export const RECORDED = {
  conversation: "01900000-0000-7000-8000-0000000000c1",
  question: "01900000-0000-7000-8000-0000000000a1",
  turn: "01900000-0000-7000-8000-000000000001",
  answer: "01900000-0000-7000-8000-000000000002",
} as const;
