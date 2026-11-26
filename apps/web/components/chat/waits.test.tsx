// An answer that waits at its model's gate says behind how many other answers it waits,
// in the place of "Waiting for the model…" (ADR 0023, revision "a turn says that it
// waits, and behind how many answers"). The number is the server's, in the turn's
// stream, one part of the message that each newer number replaces (`data-waits`); the
// rule and its words are lib/chat/waits.ts's, and tested there. Here, the line on the
// page as the recorded `waited.sse` arrives, read along with, stopped and broken off.
import { act, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { Conversation } from "@/lib/api/client";
import {
  conversation,
  fakeApi,
  heldAfter,
  json,
  listed,
  problem,
  question,
  sentByHand,
  summary,
  turn,
} from "@/tests/fake-api";
import type { Call, FakeApi } from "@/tests/fake-api";
import { ask, open } from "@/tests/open";
import { recorded, RECORDED } from "@/tests/recorded";

const TURNS = "POST /api/conversations/*/turns";
const CANCEL = "POST /api/conversations/*/turns/*/cancel";
const STREAM = "GET /api/conversations/*/turns/*/stream";
const CONVERSATION = "GET /api/conversations/*";
const LIST = "GET /api/conversations";

const QUESTION = "Is it my turn?";
/** What the model wrote, once its turn had come. */
const WORDS = "Your turn came.";
const FOR_THE_MODEL = "Waiting for the model…";
const BEHIND_TWO = "Waiting behind 2 other answers…";
const BEHIND_ONE = "Waiting behind 1 other answer…";

// The events of `waited.sse`: `start` and `start-step`, then that the answer waits behind
// two answers, behind one, and that its turn has come; then its words.
const BEGUN = 2;

/** The conversation id in a call's path. */
function conversationOf(call: Call | undefined): string {
  return call?.path.split("/")[3] ?? "";
}

/** The id of the question the page sent in its first turn. */
function sent(api: FakeApi): string {
  const body = api.to(TURNS)[0]?.body as
    { message?: { id: string } } | undefined;
  return body?.message?.id ?? "";
}

/** A recorded turn that sends its first `count` events, and loses its connection once
 * the test calls `breakOff`: what the browser's `fetch` makes of a network that went
 * away. For one request. */
function brokenByHand(
  name: string,
  count: number,
): { answer: () => Response; breakOff: () => void } {
  const first = recorded(name).split("\n\n").slice(0, count);
  let controller: ReadableStreamDefaultController<Uint8Array> | undefined;
  const body = new ReadableStream<Uint8Array>({
    start(given) {
      controller = given;
      given.enqueue(new TextEncoder().encode(first.join("\n\n") + "\n\n"));
    },
  });
  return {
    answer: () =>
      new Response(body, {
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "x-vercel-ai-ui-message-stream": "v1",
        },
      }),
    breakOff: () => {
      controller?.error(new TypeError("network error"));
    },
  };
}

/** The recorded conversation's question, while its answer waits its turn at the gate. */
function waitingItsTurn(): Conversation {
  return conversation(
    RECORDED.conversation,
    [question(RECORDED.question, QUESTION)],
    turn({ status: "running", finish_reason: null, credits: null }),
  );
}

/** The answer on the page. */
function theAnswer(): HTMLElement {
  return screen.getByRole("article", { name: "Actio" });
}

/** Give the page a moment with what has just arrived, when nothing is to change for
 * it: whatever it does with an answer takes it milliseconds. */
async function afterAMoment(): Promise<void> {
  await act(
    () =>
      new Promise<void>((resolve) => {
        setTimeout(resolve, 100);
      }),
  );
}

describe("an answer that waits its turn at its model's gate", () => {
  it("says behind how many other answers it waits, in the one line, until its words come", async () => {
    const sending = sentByHand("waited.sse");
    fakeApi().on(TURNS, sending.answer);
    const user = open();
    await ask(user, QUESTION);

    sending.send(BEGUN);
    await screen.findByRole("article", { name: "Actio" });
    const line = within(theAnswer()).getByRole("status");
    expect(line).toHaveTextContent(FOR_THE_MODEL);

    sending.send();
    await waitFor(() => {
      expect(line).toHaveTextContent(BEHIND_TWO);
    });
    sending.send();
    await waitFor(() => {
      expect(line).toHaveTextContent(BEHIND_ONE);
    });
    sending.send();
    await waitFor(() => {
      expect(line).toHaveTextContent(FOR_THE_MODEL);
    });
    // One and the same element all along, whose words changed: what a screen reader
    // is told of. And nothing else of the answer stood beside it.
    expect(within(theAnswer()).getByRole("status")).toBe(line);
    expect(theAnswer().children).toHaveLength(1);

    sending.end();

    await waitFor(() => {
      expect(theAnswer()).toHaveTextContent(WORDS);
    });
    // The answer's article is the model's words, and nothing else.
    await waitFor(() => {
      expect(theAnswer().textContent).toBe(WORDS);
    });
    expect(line).not.toBeInTheDocument();
    expect(screen.queryByText(/Waiting/)).not.toBeInTheDocument();
  });

  it.each([
    { how: "", strict: false },
    { how: ", also under React's StrictMode", strict: true },
  ])(
    "says the newest number when its conversation is opened while it waits$how",
    async ({ strict }) => {
      // The page joins the turn's stream, which it reads from its start: behind two,
      // then behind one, and nothing more yet.
      fakeApi()
        .on(CONVERSATION, () => json(200, waitingItsTurn()))
        .on(STREAM, heldAfter("waited.sse", BEGUN + 2));
      const user = open(`/c/${RECORDED.conversation}`, { strict });

      expect(await screen.findByText(BEHIND_ONE)).toBeInTheDocument();
      await afterAMoment();
      expect(within(theAnswer()).getByRole("status")).toHaveTextContent(
        BEHIND_ONE,
      );
      expect(screen.queryByText(BEHIND_TWO)).not.toBeInTheDocument();

      // The page goes elsewhere, which ends the reading: the answer waits on.
      await user.click(screen.getByRole("button", { name: "New chat" }));
      await screen.findByText("What would you like to know?");
    },
  );

  it("goes with Stop, and nothing is said of a number afterwards", async () => {
    const api = fakeApi();
    const unanswered = (call: Call, status: "running" | "stopped") =>
      conversation(
        conversationOf(call),
        [question(sent(api), QUESTION)],
        turn({ status, finish_reason: null, credits: null }),
      );
    api
      // It waits behind two, and nothing more comes.
      .on(TURNS, heldAfter("waited.sse", BEGUN + 1))
      .on(
        CONVERSATION,
        (call) => json(200, unanswered(call, "running")),
        (call) => json(200, unanswered(call, "stopped")),
      )
      .on(CANCEL, () =>
        json(200, turn({ status: "stopped", finish_reason: null })),
      );
    const user = open();
    await ask(user, QUESTION);
    await screen.findByText(BEHIND_TWO);

    await user.click(screen.getByRole("button", { name: "Stop" }));

    expect(
      await screen.findByText(
        "This answer was stopped before it was finished.",
      ),
    ).toBeInTheDocument();
    expect(api.to(CANCEL)).toHaveLength(1);
    expect(screen.queryByText(/Waiting/)).not.toBeInTheDocument();
    expect(screen.queryByRole("article", { name: "Actio" })).toBeNull();
  });

  it("leaves no number on the page when its stream breaks off and it can't be read along with", async () => {
    const breaking = brokenByHand("waited.sse", BEGUN + 1);
    const api = fakeApi();
    api
      // Behind two, and then the network is gone.
      .on(TURNS, breaking.answer)
      // The server is there again: the answer is still on its way, and can't be read.
      .on(CONVERSATION, (call) =>
        json(
          200,
          conversation(
            conversationOf(call),
            [question(sent(api), QUESTION)],
            turn({ status: "running", finish_reason: null, credits: null }),
          ),
        ),
      )
      .on(STREAM, () =>
        problem(503, "This can't be done right now. Try again in a moment."),
      );
    const user = open();
    await ask(user, QUESTION);
    await screen.findByText(BEHIND_TWO);

    breaking.breakOff();

    expect(
      await screen.findByText(/An answer to this is still being written/),
    ).toBeInTheDocument();
    expect(api.to(CONVERSATION).length).toBeGreaterThan(0);
    expect(screen.queryByText(/Waiting behind/)).not.toBeInTheDocument();
    expect(screen.queryByRole("article", { name: "Actio" })).toBeNull();
  });

  it("is in the list of conversations while it waits, when it was the first question", async () => {
    // The server has the question, and the conversation with it, from the moment it
    // says that the answer waits: the list is read then, not at the answer's first word.
    const sending = sentByHand("waited.sse");
    const api = fakeApi();
    api
      .on(TURNS, sending.answer)
      .on(LIST, () =>
        json(
          200,
          listed(
            api
              .to(TURNS)
              .map((call) => summary(conversationOf(call), QUESTION)),
          ),
        ),
      );
    const user = open();
    await screen.findByText(/No conversations yet/);
    await ask(user, QUESTION);
    sending.send(BEGUN);
    await screen.findByRole("article", { name: "Actio" });
    await afterAMoment();
    const listedBefore = api.to(LIST).length;
    const beforeItWaits = screen.queryByRole("link", { name: QUESTION });

    sending.send();

    expect(
      await screen.findByRole("link", { name: QUESTION }),
    ).toBeInTheDocument();
    expect(beforeItWaits).toBeNull();
    expect(api.to(LIST).length).toBeGreaterThan(listedBefore);
    expect(screen.getByText(BEHIND_TWO)).toBeInTheDocument();
    sending.end();
    await screen.findByText(WORDS);
  });
});
