// The chat, driven as a person would: through the page, against a fake API that
// answers with the API's recorded streams (tests/fake-api.ts).
import { act, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { Conversation, StoredMessage } from "@/lib/api/client";
import { isId } from "@/lib/ids";
import {
  answer,
  brokenAfter,
  conversation,
  fakeApi,
  GUEST,
  heldAfter,
  json,
  later,
  models,
  nothing,
  problem,
  question,
  streamed,
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
const USAGE = "GET /api/usage";
const SECOND_ANSWER = "01900000-0000-7000-8000-000000000004";
/** An exchange before the recorded one, in a stored conversation. */
const EARLIER = {
  question: "01900000-0000-7000-8000-0000000000b1",
  answer: "01900000-0000-7000-8000-0000000000b2",
};
/** What the page says of a turn that completed, and left no answer. */
const NO_ANSWER = "The model gave no answer.";

/** What the page posted to start a turn. */
interface Posted {
  message?: { id: string; parts: unknown[] };
  parent_id?: string | null;
  answer_to?: string;
  thinking: boolean;
}

function posted(call: Call | undefined): Posted {
  expect(call, "no turn was requested").toBeDefined();
  return call?.body as Posted;
}

/** The conversation id in a call's path. */
function conversationOf(call: Call | undefined): string {
  return call?.path.split("/")[3] ?? "";
}

/** The id of the question the page sent in its first turn. */
function sentQuestion(api: FakeApi): string {
  return posted(api.to(TURNS)[0]).message?.id ?? "";
}

/** The recorded conversation's question, while its answer is being written: by
 * another window, or by this page before it was reloaded. */
function beingWritten(): Conversation {
  return conversation(
    RECORDED.conversation,
    [question(RECORDED.question, "Why is the sky blue?")],
    turn({ status: "running", finish_reason: null, credits: null }),
  );
}

/** The same conversation once the answer is written. */
function written(text: string): Conversation {
  return conversation(RECORDED.conversation, [
    question(RECORDED.question, "Why is the sky blue?"),
    answer(RECORDED.answer, text, RECORDED.question),
  ]);
}

/** What the page says when a request found no server. */
const UNREACHABLE =
  "Actio's server can't be reached. Check that it is running, and your connection.";

/** Where the page reads along with the recorded turn. */
const RECORDED_STREAM = `/api/conversations/${RECORDED.conversation}/turns/${RECORDED.turn}/stream`;

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

describe("a first visit", () => {
  it("starts a guest session, and shows where to write", async () => {
    const api = fakeApi();

    open();

    expect(await screen.findByLabelText("Message")).toBeInTheDocument();
    expect(api.to("POST /api/session/guest")).toHaveLength(1);
    expect(screen.getByText(/Answered by Qwen 3.5 4B/)).toBeInTheDocument();
  });

  it("keeps the header's buttons in their places, out of reach, until the session is known", async () => {
    let start: (response: Response) => void = () => undefined;
    const api = fakeApi().on(
      "POST /api/session/guest",
      () =>
        new Promise<Response>((resolve) => {
          start = resolve;
        }),
    );

    open();

    await waitFor(() => {
      expect(api.to("POST /api/session/guest")).toHaveLength(1);
    });
    expect(screen.getByText("Starting…")).toBeInTheDocument();
    // There, so that nothing moves when they can be used. Not found by their role:
    // a person neither sees them nor reaches them.
    const list = screen.getByLabelText("Conversations", { selector: "button" });
    const newChat = screen.getByText("New chat", { selector: "button" });
    for (const button of [list, newChat]) {
      expect(button).toBeDisabled();
    }
    expect(
      screen.queryByRole("button", { name: "Conversations" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "New chat" }),
    ).not.toBeInTheDocument();

    start(json(201, GUEST));

    // The same buttons, which kept their size.
    expect(await screen.findByRole("button", { name: "Conversations" })).toBe(
      list,
    );
    expect(screen.getByRole("button", { name: "New chat" })).toBe(newChat);
    for (const button of [list, newChat]) {
      expect(button).toBeEnabled();
    }
  });

  it("says how long to wait when the network has started too many guests", async () => {
    const api = fakeApi().on(
      "POST /api/session/guest",
      () =>
        problem(
          429,
          "Too many new guests from your network. Try again in 2 minutes.",
          { retry_after: 120 },
        ),
      () => json(201, GUEST),
    );

    const user = open();

    expect(
      await screen.findByText(
        "Too many new guests from your network. Try again in 2 minutes.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Message")).not.toBeInTheDocument();
    expect(api.to("POST /api/session/guest")).toHaveLength(1); // not retried by itself

    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByLabelText("Message")).toBeInTheDocument();
  });

  it("says so when the server can't be reached", async () => {
    fakeApi().on("POST /api/session/guest", () => {
      throw new TypeError("fetch failed");
    });

    open();

    expect(
      await screen.findByText(/Actio's server can't be reached/),
    ).toBeInTheDocument();
  });
});

describe("asking a question", () => {
  it("sends it under ids the page made, and shows the answer", async () => {
    const api = fakeApi().on(TURNS, () => streamed("answer.sse"));
    const user = open();

    await ask(user, "Why is the sky blue?");

    expect(
      await screen.findByText("Echo: Why is the sky blue?"),
    ).toBeInTheDocument();
    const call = api.to(TURNS)[0];
    const body = posted(call);
    expect(body).toEqual({
      message: {
        id: body.message?.id,
        parts: [{ type: "text", text: "Why is the sky blue?" }],
      },
      parent_id: null,
      thinking: false,
    });
    expect(isId(body.message?.id ?? "")).toBe(true);
    expect(isId(conversationOf(call))).toBe(true);
    // The question is on the page, and the box is empty for the next one.
    expect(
      within(screen.getByRole("article", { name: "You" })).getByText(
        "Why is the sky blue?",
      ),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Message")).toHaveValue("");
  });

  it("gives the conversation its address once the server has it", async () => {
    const api = fakeApi().on(TURNS, () => streamed("answer.sse"));
    const user = open();
    expect(window.location.pathname).toBe("/");

    await ask(user, "Why is the sky blue?");
    await screen.findByText("Echo: Why is the sky blue?");

    expect(window.location.pathname).toBe(
      `/c/${conversationOf(api.to(TURNS)[0])}`,
    );
    // The page already has the conversation: it isn't loaded again.
    expect(api.to(CONVERSATION)).toHaveLength(0);
  });

  it("sends the next question as following the answer", async () => {
    const api = fakeApi().on(
      TURNS,
      () => streamed("answer.sse"),
      () => streamed("cut_off.sse", SECOND_ANSWER),
    );
    const user = open();

    await ask(user, "Why is the sky blue?");
    await screen.findByText("Echo: Why is the sky blue?");
    await ask(user, "Count to five.");
    await screen.findByText("One, two, three");

    const [first, second] = api.to(TURNS);
    expect(conversationOf(second)).toBe(conversationOf(first));
    expect(posted(second).parent_id).toBe(RECORDED.answer);
    expect(posted(second).message?.id).not.toBe(posted(first).message?.id);
    expect(screen.getAllByRole("article")).toHaveLength(4);
  });

  it("sends with Enter, and keeps Shift+Enter for a new line", async () => {
    const api = fakeApi().on(TURNS, () => streamed("answer.sse"));
    const user = open();

    await user.type(
      await screen.findByLabelText("Message"),
      "Two{Shift>}{Enter}{/Shift}lines{Enter}",
    );

    await screen.findByText("Echo: Why is the sky blue?");
    expect(posted(api.to(TURNS)[0]).message?.parts).toEqual([
      { type: "text", text: "Two\nlines" },
    ]);
  });

  it("sends nothing while the box is empty", async () => {
    const api = fakeApi();
    const user = open();

    await user.type(await screen.findByLabelText("Message"), "   {Enter}");

    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    expect(api.to(TURNS)).toHaveLength(0);
  });
});

describe("reasoning", () => {
  it("is asked for with the switch, and shown apart from the answer", async () => {
    const api = fakeApi().on(TURNS, () => streamed("reasoning.sse"));
    const user = open();

    await user.click(await screen.findByLabelText("Think before answering"));
    await ask(user, "Why is the sky blue?");

    expect(
      await screen.findByText("Echo: Why is the sky blue?"),
    ).toBeInTheDocument();
    expect(posted(api.to(TURNS)[0]).thinking).toBe(true);
    // Folded away once the answer is there, and still on the page to open.
    const reasoning = screen.getByText("Blue light scatters most.");
    expect(reasoning.closest("details")).not.toHaveAttribute("open");
    expect(screen.getByText("Thought before answering")).toBeInTheDocument();
  });

  it("stays switched on for the next visit", async () => {
    fakeApi();
    const user = open();

    await user.click(await screen.findByLabelText("Think before answering"));

    expect(window.localStorage.getItem("actio.thinking")).toBe("on");
  });

  it("isn't offered for a model that can't reason", async () => {
    const api = fakeApi()
      .on("GET /api/models", () => json(200, models({ thinking: false })))
      .on(TURNS, () => streamed("answer.sse"));
    window.localStorage.setItem("actio.thinking", "on"); // from an earlier model
    const user = open();

    await ask(user, "Why is the sky blue?");
    await screen.findByText("Echo: Why is the sky blue?");

    expect(
      screen.queryByLabelText("Think before answering"),
    ).not.toBeInTheDocument();
    expect(posted(api.to(TURNS)[0]).thinking).toBe(false);
  });
});

describe("markdown", () => {
  // What markdown becomes, and what it may not do, is in markdown.test.tsx. Here: that
  // the chat shows answers through it.

  it("shows an answer as markdown while it arrives, and what was kept of it", async () => {
    // The recorded answer's first twelve events end in the middle of what is bold:
    // "Echo: Name **two ". Its reasoning, "A *bike* has parts.", is whole by then.
    const api = fakeApi();
    api
      .on(TURNS, heldAfter("markdown.sse", 12))
      .on(CONVERSATION, (call) =>
        json(
          200,
          conversation(conversationOf(call), [
            question(sentQuestion(api), "Name **two bike** parts."),
            answer(
              RECORDED.answer,
              "Echo: Name **two ",
              sentQuestion(api),
              turn({ status: "stopped", finish_reason: null }),
            ),
          ]),
        ),
      );
    const user = open();

    await user.click(await screen.findByLabelText("Think before answering"));
    await ask(user, "Name **two bike** parts.");

    expect(await screen.findByText("two")).toHaveClass("font-semibold");
    expect(screen.getByText("bike").tagName).toBe("EM");
    const arriving = screen.getByRole("article", { name: "Actio" });
    expect(arriving).toHaveTextContent("Echo: Name two");
    expect(arriving).not.toHaveTextContent("*");
    // The question is shown as it was typed.
    expect(screen.getByRole("article", { name: "You" })).toHaveTextContent(
      "Name **two bike** parts.",
    );

    await user.click(screen.getByRole("button", { name: "Stop" }));

    await screen.findByText("This answer was stopped before it was finished.");
    expect(screen.getByText("two")).toHaveClass("font-semibold");
    expect(
      screen.getByRole("article", { name: "Actio" }),
    ).not.toHaveTextContent("*");
  });

  it("shows a stored answer as markdown", async () => {
    fakeApi().on(CONVERSATION, () =>
      json(
        200,
        conversation(RECORDED.conversation, [
          question(RECORDED.question, "Which parts?"),
          answer(
            RECORDED.answer,
            "## Parts\n\n1. The `saddle`\n2. The chain",
            RECORDED.question,
          ),
        ]),
      ),
    );

    open(`/c/${RECORDED.conversation}`);

    expect(
      await screen.findByRole("heading", { level: 2, name: "Parts" }),
    ).toBeVisible();
    expect(
      within(screen.getByRole("article", { name: "Actio" }))
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(["The saddle", "The chain"]);
  });
});

describe("an answer that doesn't arrive whole", () => {
  it("explains a model's failure, and tries again when asked", async () => {
    const api = fakeApi();
    api
      .on(
        TURNS,
        () => streamed("model_unavailable.sse"),
        () => streamed("answer.sse"),
      )
      .on(CONVERSATION, (call) =>
        json(
          200,
          conversation(
            conversationOf(call),
            [question(sentQuestion(api), "Why is the sky blue?")],
            turn({ status: "failed", error: "model_unavailable" }),
          ),
        ),
      );
    const user = open();

    await ask(user, "Why is the sky blue?");

    expect(
      await screen.findByText(/can't be reached, or is too busy/),
    ).toBeInTheDocument();
    // The question was stored, so it stays, and the conversation has its address.
    expect(screen.getAllByRole("article")).toHaveLength(1);
    await waitFor(() => {
      expect(window.location.pathname).toMatch(/^\/c\//);
    });

    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(
      await screen.findByText("Echo: Why is the sky blue?"),
    ).toBeInTheDocument();
    expect(posted(api.to(TURNS)[1])).toEqual({
      answer_to: sentQuestion(api),
      thinking: false,
    });
    expect(screen.queryByText(/too busy/)).not.toBeInTheDocument();
  });

  it("says when the model gave no answer, and asks it again when asked", async () => {
    // The model answered with nothing, and ended as a finished answer does. The
    // server stores no answer then, and the question stands open.
    const api = fakeApi();
    api.on(
      TURNS,
      () => streamed("no_words.sse"),
      () => streamed("answer.sse"),
    );
    const user = open();

    await ask(user, "Why is the sky blue?");

    expect(await screen.findByText(NO_ANSWER)).toBeVisible();
    // An answer with nothing in it isn't drawn: the question stands alone.
    expect(screen.getAllByRole("article")).toHaveLength(1);

    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(
      await screen.findByText("Echo: Why is the sky blue?"),
    ).toBeInTheDocument();
    expect(posted(api.to(TURNS)[1])).toEqual({
      answer_to: sentQuestion(api),
      thinking: false,
    });
    // An answer that has words says nothing of the sort, and isn't asked for again:
    // looked at once it is over, since nothing of the kind is said while one arrives.
    expect(await screen.findByRole("button", { name: "Send" })).toBeVisible();
    expect(screen.queryByText(NO_ANSWER)).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Try again" }),
    ).not.toBeInTheDocument();
  });

  it("says the same of a stored question whose turn completed without an answer", async () => {
    const api = fakeApi();
    api
      .on(CONVERSATION, () =>
        json(
          200,
          conversation(
            RECORDED.conversation,
            [question(RECORDED.question, "Why is the sky blue?")],
            turn({ output_tokens: 0, first_word_at: null }),
          ),
        ),
      )
      .on(TURNS, () => streamed("answer.sse"));
    const user = open(`/c/${RECORDED.conversation}`);

    expect(await screen.findByText(NO_ANSWER)).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(
      await screen.findByText("Echo: Why is the sky blue?"),
    ).toBeInTheDocument();
    expect(posted(api.to(TURNS)[0])).toEqual({
      answer_to: RECORDED.question,
      thinking: false,
    });
  });

  it("says so too when the model thought, and then gave no answer", async () => {
    const thought: StoredMessage = {
      ...answer(RECORDED.answer, "", RECORDED.question),
      parts: [{ type: "reasoning", text: "Something to sit on." }],
    };
    const api = fakeApi();
    api
      .on(CONVERSATION, () =>
        json(
          200,
          conversation(RECORDED.conversation, [
            question(RECORDED.question, "Why is the sky blue?"),
            thought,
          ]),
        ),
      )
      .on(TURNS, () => streamed("answer.sse"));

    const user = open(`/c/${RECORDED.conversation}`);

    expect(await screen.findByText(NO_ANSWER)).toBeVisible();
    // What it thought is there to read.
    expect(screen.getByText("Thought before answering")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Try again" }));

    // Another answer to the same question, beside the one that only thought.
    expect(
      await screen.findByText("Echo: Why is the sky blue?"),
    ).toBeInTheDocument();
    expect(posted(api.to(TURNS)[0])).toEqual({
      answer_to: RECORDED.question,
      thinking: false,
    });
  });

  it("takes an answer of nothing but blank space for no answer, and doesn't draw it", async () => {
    // A model can write a line break and no more, and the server stores that.
    fakeApi().on(CONVERSATION, () =>
      json(
        200,
        conversation(RECORDED.conversation, [
          question(RECORDED.question, "Why is the sky blue?"),
          answer(RECORDED.answer, "\n\n", RECORDED.question),
        ]),
      ),
    );

    open(`/c/${RECORDED.conversation}`);

    expect(await screen.findByText(NO_ANSWER)).toBeVisible();
    expect(screen.getByRole("button", { name: "Try again" })).toBeEnabled();
    // Nothing to read in it: only the question is on the page.
    expect(screen.getAllByRole("article")).toHaveLength(1);
  });

  it("offers another try when the model reached its length limit before a word", async () => {
    fakeApi().on(CONVERSATION, () =>
      json(
        200,
        conversation(
          RECORDED.conversation,
          [question(RECORDED.question, "Count to five.")],
          turn({
            finish_reason: "length",
            output_tokens: 0,
            first_word_at: null,
          }),
        ),
      ),
    );

    open(`/c/${RECORDED.conversation}`);

    expect(
      await screen.findByText(/reached the model's length limit/),
    ).toBeVisible();
    expect(screen.queryByText(NO_ANSWER)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeEnabled();
  });

  it("keeps the beginning of an answer that broke off", async () => {
    const api = fakeApi();
    api
      .on(TURNS, () => streamed("broke_off.sse"))
      .on(CONVERSATION, (call) =>
        json(
          200,
          conversation(conversationOf(call), [
            question(sentQuestion(api), "Say one two three."),
            answer(
              RECORDED.answer,
              "Echo: one two ",
              sentQuestion(api),
              turn({ status: "failed", error: "answer_interrupted" }),
            ),
          ]),
        ),
      );
    const user = open();

    await ask(user, "Say one two three.");

    expect(await screen.findByText(/The answer broke off/)).toBeInTheDocument();
    expect(screen.getByText("Echo: one two")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Try again" }),
    ).toBeInTheDocument();
  });

  it("shows what the server kept of an answer that arrived whole after the server had given up on it", async () => {
    // Its process was only slow, and the server gave up on the turn: the answer as its
    // last save held it is kept, and the turn was interrupted. What arrived after that
    // isn't the server's, so the stream ends in an error in place of `finish`.
    const api = fakeApi();
    const earlier = [
      question(EARLIER.question, "Name a bike part."),
      answer(
        EARLIER.answer,
        "The saddle.",
        EARLIER.question,
        turn({ context_tokens: 3_214 }),
      ),
    ];
    const givenUp = turn({
      status: "interrupted",
      finish_reason: null,
      input_tokens: null,
      cached_input_tokens: null,
      output_tokens: null,
      credits: null,
      first_word_at: null,
    });
    api
      .on(TURNS, () => streamed("given_up.sse"))
      .on(
        CONVERSATION,
        () => json(200, conversation(RECORDED.conversation, earlier)),
        () =>
          json(
            200,
            conversation(RECORDED.conversation, [
              ...earlier,
              question(sentQuestion(api), "Are you there?", EARLIER.answer),
              answer(RECORDED.answer, "Sorry, ", sentQuestion(api), givenUp),
            ]),
          ),
      );
    const user = open(`/c/${RECORDED.conversation}`);
    await screen.findByText("The saddle.");

    await ask(user, "Are you there?");

    expect(
      await screen.findByText(
        "The server was restarted while this answer was being written.",
      ),
    ).toBeVisible();
    expect(api.to(CONVERSATION)).toHaveLength(2);
    const [before, kept] = screen.getAllByRole("article", { name: "Actio" });
    // The answer as it was kept, shorter than what arrived.
    expect(kept).toHaveTextContent(/^Sorry,$/);
    expect(screen.queryByText(/I'm late/)).not.toBeInTheDocument();
    // What the earlier answer took is said under it, and nothing under this one: its
    // turn counted nothing. The window holds what it held before it.
    const took = screen.getAllByRole("list", { name: "What this answer took" });
    expect(took).toHaveLength(1);
    expect(before?.nextElementSibling).toBe(took[0]);
    expect(screen.getByRole("meter", { name: "Context" })).toHaveAttribute(
      "aria-valuetext",
      "39% full: 3,214 of 8,192 tokens",
    );
    expect(screen.getByRole("button", { name: "Try again" })).toBeEnabled();
  });

  it("says when an answer ran into the model's length limit", async () => {
    fakeApi().on(TURNS, () => streamed("cut_off.sse"));
    const user = open();

    await ask(user, "Count to five.");

    expect(
      await screen.findByText(/reached the model's length limit/),
    ).toBeInTheDocument();
    expect(screen.getByText("One, two, three")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Try again" }),
    ).not.toBeInTheDocument();
  });

  it("says when it was a usage limit that left an answer no more room", async () => {
    // Near a limit the API gives an answer less room than the model allows, and the
    // turn says so.
    const cramped = turn({ finish_reason: "length", capped_by_usage: true });
    fakeApi().on(CONVERSATION, () =>
      json(
        200,
        conversation(RECORDED.conversation, [
          question(RECORDED.question, "Count to a hundred."),
          answer(RECORDED.answer, "One, two", RECORDED.question, cramped),
        ]),
      ),
    );

    open(`/c/${RECORDED.conversation}`);

    expect(
      await screen.findByText(
        "This answer stops here: your usage limit left no room for more of it.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/reached the model's length limit/),
    ).not.toBeInTheDocument();
  });

  it("stops when asked, and shows what the server kept of it", async () => {
    const api = fakeApi();
    const kept = (call: Call, by: Parameters<typeof turn>[0], text?: string) =>
      conversation(
        conversationOf(call),
        [
          question(sentQuestion(api), "Why is the sky blue?"),
          ...(text === undefined
            ? []
            : [answer(RECORDED.answer, text, sentQuestion(api), turn(by))]),
        ],
        text === undefined ? turn(by) : null,
      );
    api
      // The start of the answer and its first words arrive; then nothing.
      .on(TURNS, heldAfter("answer.sse", 5))
      .on(
        CONVERSATION,
        // The page has stopped reading. At the server the answer is written on.
        (call) =>
          json(200, kept(call, { status: "running", finish_reason: null })),
        (call) =>
          json(
            200,
            kept(call, { status: "stopped", finish_reason: null }, "Echo: Why"),
          ),
      )
      // So the page stops the turn, and the API answers once that is recorded.
      .on(CANCEL, () =>
        json(200, turn({ status: "stopped", finish_reason: null })),
      );
    const user = open();

    await ask(user, "Why is the sky blue?");
    await screen.findByText(/^Echo:/);
    const usageRead = api.to("GET /api/usage").length;
    await user.click(screen.getByRole("button", { name: "Stop" }));

    expect(api.to(TURNS)[0]?.signal?.aborted).toBe(true);
    expect(
      await screen.findByText(
        "This answer was stopped before it was finished.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Echo: Why")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument();
    // The turn that was stopped is the one the conversation said was running.
    expect(api.to(CANCEL).map((call) => call.path)).toEqual([
      `/api/conversations/${conversationOf(api.to(TURNS)[0])}/turns/${RECORDED.turn}/cancel`,
    ]);
    expect(api.to(CONVERSATION)).toHaveLength(2);
    // A stopped answer is paid for as far as it got: the counts are read after it.
    expect(api.to("GET /api/usage").length).toBeGreaterThan(usageRead + 1);
  });

  it("shows an answer that was stopped from elsewhere as it ended, without asking again", async () => {
    // Another window stopped it, or the conversation was deleted there. The stream
    // this page was reading ends as a whole answer's does, and says how in its last
    // chunk. Nothing more is read: a request for the conversation would have no
    // answer here, and fail the test.
    fakeApi().on(TURNS, () => streamed("stopped.sse"));
    const user = open();

    await ask(user, "Count to a hundred.");

    expect(
      await screen.findByText(
        "This answer was stopped before it was finished.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("article", { name: "Actio" })).toHaveTextContent(
      "One, two, three",
    );
    expect(screen.getByRole("button", { name: "Try again" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument();
  });

  it("stops an answer of which nothing has arrived yet, by what the server says is running", async () => {
    // The page has heard nothing of the turn: not even its id. The server has it.
    const running = "01900000-0000-7000-8000-0000000000f1";
    const api = fakeApi();
    const unanswered = (call: Call, status: "running" | "stopped") =>
      conversation(
        conversationOf(call),
        [question(sentQuestion(api), "Why is the sky blue?")],
        turn({ id: running, status, finish_reason: null, credits: null }),
      );
    api
      .on(TURNS, heldAfter("answer.sse", 0))
      .on(
        CONVERSATION,
        (call) => json(200, unanswered(call, "running")),
        (call) => json(200, unanswered(call, "stopped")),
      )
      .on(CANCEL, () =>
        json(
          200,
          turn({ id: running, status: "stopped", finish_reason: null }),
        ),
      );
    const user = open();

    await ask(user, "Why is the sky blue?");
    await user.click(await screen.findByRole("button", { name: "Stop" }));

    expect(
      await screen.findByText(
        "This answer was stopped before it was finished.",
      ),
    ).toBeInTheDocument();
    expect(api.to(CANCEL).map((call) => call.path.split("/")[5])).toEqual([
      running,
    ]);
    expect(screen.getByRole("article", { name: "You" })).toHaveTextContent(
      "Why is the sky blue?",
    );
    expect(screen.getByRole("button", { name: "Try again" })).toBeEnabled();
  });

  it("says so when an answer couldn't be stopped, and reads along with it to its end", async () => {
    const api = fakeApi();
    const writing = (call: Call) =>
      conversation(
        conversationOf(call),
        [question(sentQuestion(api), "Why is the sky blue?")],
        turn({ status: "running", finish_reason: null }),
      );
    api
      .on(TURNS, heldAfter("answer.sse", 5))
      .on(CONVERSATION, (call) => json(200, writing(call)))
      .on(CANCEL, () =>
        problem(503, "This can't be done right now. Try again in a moment."),
      )
      // It is written on, and the page reads along with it again: from its start.
      .on(STREAM, () => streamed("answer.sse"));
    const user = open();

    await ask(user, "Why is the sky blue?");
    await screen.findByText(/^Echo:/);
    await user.click(screen.getByRole("button", { name: "Stop" }));

    expect(
      await screen.findByText(
        "This can't be done right now. Try again in a moment.",
      ),
    ).toBeInTheDocument();
    expect(
      await screen.findByText("Echo: Why is the sky blue?"),
    ).toBeInTheDocument();
    expect(api.to(CANCEL)).toHaveLength(1);
    expect(api.to(STREAM).map((call) => call.path)).toEqual([
      `/api/conversations/${conversationOf(api.to(TURNS)[0])}/turns/${RECORDED.turn}/stream`,
    ]);
    expect(api.to(CONVERSATION)).toHaveLength(1);
    expect(screen.queryByText(/still being written/)).not.toBeInTheDocument();
    // The refusal stays: the answer went on, which is what it says.
    expect(
      screen.getByText("This can't be done right now. Try again in a moment."),
    ).toBeInTheDocument();
  });

  it("sends the next question after a message the server has, however soon it is sent", async () => {
    // A failed answer's stream begins like any other, so for a moment the page holds
    // an answer that the server never stored. The page learns that when it reads the
    // conversation again, and here that takes a while.
    let tell: () => void = () => undefined;
    const serverTold = new Promise<void>((resolve) => {
      tell = resolve;
    });
    const api = fakeApi();
    api
      .on(
        TURNS,
        () => streamed("model_unavailable.sse"),
        () => streamed("answer.sse"),
      )
      .on(CONVERSATION, async (call) => {
        await serverTold;
        return json(
          200,
          conversation(
            conversationOf(call),
            [question(sentQuestion(api), "Why is the sky blue?")],
            turn({ status: "failed", error: "model_unavailable" }),
          ),
        );
      });
    const user = open();

    await ask(user, "Why is the sky blue?");
    await screen.findByText(/can't be reached, or is too busy/);
    expect(api.to(CONVERSATION)).toHaveLength(1); // asked, and not yet told
    await ask(user, "Why is grass green?");

    await screen.findByText("Echo: Why is the sky blue?");
    const second = posted(api.to(TURNS)[1]);
    expect(second).toEqual({
      message: {
        id: second.message?.id,
        parts: [{ type: "text", text: "Why is grass green?" }],
      },
      // The conversation's first question again: it takes the place of the one that
      // got no answer. Not a question after an answer that doesn't exist.
      parent_id: null,
      thinking: false,
    });
    expect(screen.getAllByRole("article", { name: "You" })).toHaveLength(1);

    // What the server says about the earlier state arrives late, and changes nothing.
    tell();
    await waitFor(() => {
      expect(
        screen.getByText("Echo: Why is the sky blue?"),
      ).toBeInTheDocument();
    });
    expect(screen.queryByText(/too busy/)).not.toBeInTheDocument();
  });
});

describe("a question the API refuses", () => {
  it("goes back into the box, with the reason", async () => {
    const api = fakeApi()
      .on(TURNS, () =>
        problem(
          429,
          "Too many messages in a short time. Try again in 40 seconds.",
          { retry_after: 40 },
        ),
      )
      .on(CONVERSATION, () => problem(404, "No such conversation."));
    const user = open();

    await ask(user, "Why is the sky blue?");

    expect(
      await screen.findByText(
        "Too many messages in a short time. Try again in 40 seconds.",
      ),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByLabelText("Message")).toHaveValue(
        "Why is the sky blue?",
      );
    });
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
    // Nothing was stored, so there is no conversation to give an address to.
    expect(window.location.pathname).toBe("/");
    expect(api.to(TURNS)).toHaveLength(1);

    await user.click(screen.getByRole("button", { name: "Dismiss" }));

    expect(screen.queryByText(/Too many messages/)).not.toBeInTheDocument();
  });

  it("says so when the session has ended, and starts a new one when asked", async () => {
    const ended = () =>
      problem(
        401,
        "This needs a session. Start one with POST /api/session/guest.",
      );
    const api = fakeApi().on(TURNS, ended).on(CONVERSATION, ended);
    const user = open();

    await ask(user, "Why is the sky blue?");

    expect(
      await screen.findByText("Your session has ended."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();

    await user.click(
      screen.getByRole("button", { name: "Start a new session" }),
    );

    await waitFor(() => {
      expect(api.to("POST /api/session/guest")).toHaveLength(2);
    });
    expect(
      screen.queryByText("Your session has ended."),
    ).not.toBeInTheDocument();
  });
});

describe("a model that can't answer", () => {
  it("is explained, with the command that downloads it", async () => {
    const api = fakeApi().on("GET /api/models", () =>
      json(200, models({ status: "missing", thinking: false })),
    );
    const user = open();

    expect(
      await screen.findByText("Qwen 3.5 4B isn't downloaded yet."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("ollama pull qwen3.5:4b-q4_K_M"),
    ).toBeInTheDocument();

    await user.type(screen.getByLabelText("Message"), "Hello{Enter}");

    // What was written stays; nothing is sent to be refused.
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    expect(screen.getByLabelText("Message")).toHaveValue("Hello");
    expect(api.to(TURNS)).toHaveLength(0);
  });

  it("is explained when its server doesn't answer", async () => {
    fakeApi().on("GET /api/models", () =>
      json(200, models({ status: "unreachable", thinking: false })),
    );

    open();

    expect(
      await screen.findByText("Qwen 3.5 4B can't be reached."),
    ).toBeInTheDocument();
    expect(screen.getByText(/Start Ollama/)).toBeInTheDocument();
  });
});

describe("a conversation's address", () => {
  it("shows the stored conversation", async () => {
    const api = fakeApi().on(
      CONVERSATION,
      () =>
        new Response(recorded("conversation.json"), {
          headers: { "content-type": "application/json" },
        }),
    );

    open(`/c/${RECORDED.conversation}`);

    expect(
      await screen.findByText("Echo: Why is the sky blue?"),
    ).toBeInTheDocument();
    expect(api.to(CONVERSATION)[0]?.path).toBe(
      `/api/conversations/${RECORDED.conversation}`,
    );
    const [asked, answered] = screen.getAllByRole("article");
    expect(asked).toHaveAccessibleName("You");
    expect(asked).toHaveTextContent("Why is the sky blue?");
    expect(answered).toHaveAccessibleName("Actio");
    expect(
      screen.getByText("Blue light scatters most.").closest("details"),
    ).not.toHaveAttribute("open");
  });

  it("continues the stored conversation from its last answer", async () => {
    const api = fakeApi()
      .on(
        CONVERSATION,
        () =>
          new Response(recorded("conversation.json"), {
            headers: { "content-type": "application/json" },
          }),
      )
      .on(TURNS, () => streamed("cut_off.sse", SECOND_ANSWER));
    const user = open(`/c/${RECORDED.conversation}`);

    await screen.findByText("Echo: Why is the sky blue?");
    await ask(user, "Count to five.");
    await screen.findByText("One, two, three");

    const call = api.to(TURNS)[0];
    expect(conversationOf(call)).toBe(RECORDED.conversation);
    expect(posted(call).parent_id).toBe(RECORDED.answer);
  });

  it("offers to answer a stored question that never got an answer", async () => {
    const api = fakeApi()
      .on(CONVERSATION, () =>
        json(
          200,
          conversation(
            RECORDED.conversation,
            [question(RECORDED.question, "Why is the sky blue?")],
            turn({ status: "interrupted", finish_reason: null }),
          ),
        ),
      )
      .on(TURNS, () => streamed("answer.sse"));
    const user = open(`/c/${RECORDED.conversation}`);

    expect(
      await screen.findByText(
        "The server was restarted while this answer was being written.",
      ),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Try again" }));

    await screen.findByText("Echo: Why is the sky blue?");
    expect(posted(api.to(TURNS)[0])).toEqual({
      answer_to: RECORDED.question,
      thinking: false,
    });
  });

  it("replaces a question that never got an answer with a reworded one", async () => {
    const api = fakeApi()
      .on(CONVERSATION, () =>
        json(
          200,
          conversation(
            RECORDED.conversation,
            [
              question("q1", "Hello"),
              answer("a1", "Hi.", "q1"),
              question("q2", "Why is teh sky blue?", "a1"),
            ],
            turn({ status: "failed", error: "model_error" }),
          ),
        ),
      )
      .on(TURNS, () => streamed("answer.sse"));
    const user = open(`/c/${RECORDED.conversation}`);

    await screen.findByText("Why is teh sky blue?");
    await ask(user, "Why is the sky blue?");
    await screen.findByText("Echo: Why is the sky blue?");

    // Both versions of the question follow the same answer.
    expect(posted(api.to(TURNS)[0]).parent_id).toBe("a1");
    expect(screen.queryByText("Why is teh sky blue?")).not.toBeInTheDocument();
  });

  it("reads along with an answer that another window is writing, to its end", async () => {
    const reading = later(() => streamed("answer.sse"));
    const api = fakeApi()
      .on(CONVERSATION, () => json(200, beingWritten()))
      .on(STREAM, reading.answer);
    const user = open(`/c/${RECORDED.conversation}`);
    await screen.findByText("Waiting for the model…");
    const usageRead = api.to(USAGE).length;
    const listRead = api.to(LIST).length;

    reading.release();

    expect(
      await screen.findByText("Echo: Why is the sky blue?"),
    ).toBeInTheDocument();
    await user.type(screen.getByLabelText("Message"), "And at sunset?");
    expect(await screen.findByRole("button", { name: "Send" })).toBeEnabled();
    expect(screen.queryByText(/still being written/)).not.toBeInTheDocument();
    // Nothing more is asked about that answer: it is here, and its turn with it.
    expect(api.to(CONVERSATION)).toHaveLength(1);
    expect(api.to(STREAM).map((call) => call.path)).toEqual([RECORDED_STREAM]);
    // What it cost, and where its conversation stands in the list, are read again.
    await waitFor(() => {
      expect(api.to(USAGE).length).toBeGreaterThan(usageRead);
    });
    expect(api.to(LIST).length).toBeGreaterThan(listRead);
  });

  it("says when there is no such conversation, and offers a new one", async () => {
    fakeApi().on(CONVERSATION, () => problem(404, "No such conversation."));
    const user = open(`/c/${RECORDED.conversation}`);

    expect(
      await screen.findByText(
        "This conversation doesn’t exist, or belongs to another session.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Message")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Start a new chat" }));

    expect(await screen.findByLabelText("Message")).toBeInTheDocument();
    expect(window.location.pathname).toBe("/");
  });

  it("can be tried again when loading it failed", async () => {
    fakeApi().on(
      CONVERSATION,
      () => {
        throw new TypeError("fetch failed");
      },
      () =>
        new Response(recorded("conversation.json"), {
          headers: { "content-type": "application/json" },
        }),
    );
    const user = open(`/c/${RECORDED.conversation}`);

    await user.click(await screen.findByRole("button", { name: "Try again" }));

    expect(
      await screen.findByText("Echo: Why is the sky blue?"),
    ).toBeInTheDocument();
  });
});

describe("an answer that is being written, and that this page isn't reading", () => {
  // Another window asked for it, or this page did and was then reloaded. The page
  // reads along with it, once; when that can't be done, it looks every so often.

  it("is shown as it arrives once the page reads along, and is written on when the page goes elsewhere", async () => {
    const reading = later(heldAfter("answer.sse", 5));
    const api = fakeApi()
      .on(CONVERSATION, () => json(200, beingWritten()))
      .on(STREAM, reading.answer);
    const user = open(`/c/${RECORDED.conversation}`);
    const log = () => screen.getByRole("log", { name: "Conversation" });

    // While the stream is asked for: as right after a question was sent.
    expect(
      await screen.findByText("Waiting for the model…"),
    ).toBeInTheDocument();
    expect(log()).toHaveAttribute("aria-busy", "true");
    await user.type(screen.getByLabelText("Message"), "And at sunset?");
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Stop" })).toBeNull();
    expect(screen.queryByText(/still being written/)).not.toBeInTheDocument();

    reading.release();

    expect(await screen.findByText(/^Echo: Why/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();
    expect(log()).toHaveAttribute("aria-busy", "true");
    expect(screen.queryByText("Waiting for the model…")).toBeNull();
    expect(screen.queryByText(/still being written/)).not.toBeInTheDocument();
    expect(api.to(STREAM).map((call) => call.path)).toEqual([RECORDED_STREAM]);

    await user.click(screen.getByRole("button", { name: "New chat" }));

    expect(
      await screen.findByText("What would you like to know?"),
    ).toBeInTheDocument();
    // The page stopped reading. It asked nobody to stop writing: a request for that
    // would have no answer here, and fail the test. Nor did it look again.
    expect(api.to(STREAM)[0]?.signal?.aborted).toBe(true);
    expect(api.to(CONVERSATION)).toHaveLength(1);
  });

  it("is stopped by Stop, as the turn the server says is running, and shows what was kept", async () => {
    const stopped = turn({ status: "stopped", finish_reason: null });
    const api = fakeApi()
      .on(
        CONVERSATION,
        () => json(200, beingWritten()),
        // The page has stopped reading. At the server the answer is written on.
        () => json(200, beingWritten()),
        () =>
          json(
            200,
            conversation(RECORDED.conversation, [
              question(RECORDED.question, "Why is the sky blue?"),
              answer(RECORDED.answer, "Echo: Why", RECORDED.question, stopped),
            ]),
          ),
      )
      .on(STREAM, heldAfter("answer.sse", 5))
      .on(CANCEL, () => json(200, stopped));
    const user = open(`/c/${RECORDED.conversation}`);
    await screen.findByText(/^Echo:/);

    await user.click(screen.getByRole("button", { name: "Stop" }));

    expect(api.to(STREAM)[0]?.signal?.aborted).toBe(true);
    expect(
      await screen.findByText(
        "This answer was stopped before it was finished.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Echo: Why")).toBeInTheDocument();
    expect(api.to(CANCEL).map((call) => call.path)).toEqual([
      `/api/conversations/${RECORDED.conversation}/turns/${RECORDED.turn}/cancel`,
    ]);
    expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument();
    expect(screen.queryByText(/still being written/)).not.toBeInTheDocument();
    expect(api.to(STREAM)).toHaveLength(1);
  });

  it("is shown as it was left when it ended before the page could read along", async () => {
    // The server answers that there is nothing to read along with any more.
    const reading = later(nothing);
    const api = fakeApi()
      .on(
        CONVERSATION,
        () => json(200, beingWritten()),
        () => json(200, written("Because of scattering.")),
      )
      .on(STREAM, reading.answer);
    open(`/c/${RECORDED.conversation}`);
    await screen.findByText("Waiting for the model…");
    const usageRead = api.to(USAGE).length;
    const listRead = api.to(LIST).length;

    reading.release();

    expect(
      await screen.findByText("Because of scattering."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Waiting for the model…")).toBeNull();
    expect(screen.queryByText(/still being written/)).not.toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Send" })).toBeVisible();
    expect(api.to(CONVERSATION)).toHaveLength(2);
    expect(api.to(STREAM)).toHaveLength(1);
    // It ended without this page reading it: what it cost, and where its
    // conversation stands in the list, are read now.
    await waitFor(() => {
      expect(api.to(USAGE).length).toBeGreaterThan(usageRead);
    });
    expect(api.to(LIST).length).toBeGreaterThan(listRead);
  });

  it("is looked for until it is done when its stream was lost, and not read along with again", async () => {
    const api = fakeApi()
      .on(
        CONVERSATION,
        () => json(200, beingWritten()),
        () => json(200, beingWritten()),
        () => json(200, written("Because of scattering.")),
      )
      .on(STREAM, () => streamed("lost.sse"));
    open(`/c/${RECORDED.conversation}`);

    expect(
      await screen.findByText(/An answer to this is still being written/),
    ).toBeInTheDocument();
    expect(
      await screen.findByText("Because of scattering.", {}, { timeout: 4000 }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/still being written/)).not.toBeInTheDocument();
    // What the stream said is the conversation's to say, and the page read that.
    expect(screen.queryByText(/connection to this answer was lost/)).toBeNull();
    expect(api.to(STREAM)).toHaveLength(1);
    expect(api.to(CONVERSATION)).toHaveLength(3);
  });

  it.each([
    [
      "refused",
      () =>
        problem(503, "This can't be done right now. Try again in a moment."),
    ],
    [
      "lost on the way",
      () => {
        throw new TypeError("fetch failed");
      },
    ],
  ])(
    "is looked for until it is done when the stream was %s, and nothing is said of that",
    async (_, failing) => {
      const api = fakeApi()
        .on(
          CONVERSATION,
          () => json(200, beingWritten()),
          () => json(200, beingWritten()),
          () => json(200, written("Because of scattering.")),
        )
        .on(STREAM, failing);
      const user = open(`/c/${RECORDED.conversation}`);

      expect(
        await screen.findByText(/An answer to this is still being written/),
      ).toBeInTheDocument();
      // The box is blocked, as for any answer that is being written.
      await user.type(screen.getByLabelText("Message"), "And at sunset?");
      expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
      expect(
        await screen.findByText(
          "Because of scattering.",
          {},
          { timeout: 4000 },
        ),
      ).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Send" })).toBeEnabled();
      expect(screen.queryByText(/can't be done right now/)).toBeNull();
      expect(screen.queryByText(/can't be reached/)).toBeNull();
      expect(screen.queryByRole("button", { name: "Dismiss" })).toBeNull();
      expect(api.to(STREAM)).toHaveLength(1);
    },
  );

  it("is shown as stopped when it was stopped elsewhere, without asking again", async () => {
    // The stream ends as a whole answer's does, and says how in its last chunk. A
    // read of the conversation would find it as running here, and say so.
    const api = fakeApi()
      .on(CONVERSATION, () => json(200, beingWritten()))
      .on(STREAM, () => streamed("stopped.sse"));
    open(`/c/${RECORDED.conversation}`);

    expect(
      await screen.findByText(
        "This answer was stopped before it was finished.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("article", { name: "Actio" })).toHaveTextContent(
      "One, two, three",
    );
    expect(screen.getByRole("button", { name: "Try again" })).toBeEnabled();
    expect(screen.queryByText(/still being written/)).not.toBeInTheDocument();
    expect(api.to(CONVERSATION)).toHaveLength(1);
    expect(api.to(STREAM)).toHaveLength(1);
  });

  it("is read through the line that keeps a silent connection open", async () => {
    const api = fakeApi()
      .on(CONVERSATION, () => json(200, beingWritten()))
      .on(STREAM, () => streamed("keep_alive.sse"));
    open(`/c/${RECORDED.conversation}`);

    expect(
      await screen.findByText("Sorry to keep you waiting."),
    ).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Send" })).toBeVisible();
    expect(screen.queryByText(/still being written/)).not.toBeInTheDocument();
    expect(api.to(CONVERSATION)).toHaveLength(1);
  });

  it("is this page's own, read along with again when its connection broke off", async () => {
    const reading = later(() => streamed("answer.sse"));
    const api = fakeApi();
    api
      // The first words arrive; then the network is gone for a moment.
      .on(TURNS, () => brokenAfter("answer.sse", 5))
      // The server is there again, and the answer is being written on.
      .on(CONVERSATION, (call) =>
        json(
          200,
          conversation(
            conversationOf(call),
            [question(sentQuestion(api), "Why is the sky blue?")],
            turn({ status: "running", finish_reason: null }),
          ),
        ),
      )
      .on(STREAM, reading.answer);
    const user = open();

    await ask(user, "Why is the sky blue?");

    // While the page asks to read along, the break is what it knows of.
    expect(await screen.findByText(UNREACHABLE)).toBeInTheDocument();
    expect(
      await screen.findByText("Waiting for the model…"),
    ).toBeInTheDocument();

    reading.release();

    expect(
      await screen.findByText("Echo: Why is the sky blue?"),
    ).toBeInTheDocument();
    // The server was reached after all: what the notice said is no longer true.
    expect(screen.queryByText(UNREACHABLE)).toBeNull();
    expect(screen.queryByRole("button", { name: "Dismiss" })).toBeNull();
    expect(screen.getAllByRole("article")).toHaveLength(2);
    expect(api.to(STREAM).map((call) => call.path)).toEqual([
      `/api/conversations/${conversationOf(api.to(TURNS)[0])}/turns/${RECORDED.turn}/stream`,
    ]);
    expect(api.to(CONVERSATION)).toHaveLength(1);
    expect(screen.queryByText(/still being written/)).not.toBeInTheDocument();
  });

  it("takes back what it said of a broken connection as soon as it reads along again", async () => {
    const api = fakeApi();
    api
      .on(TURNS, () => brokenAfter("answer.sse", 5))
      .on(CONVERSATION, (call) =>
        json(
          200,
          conversation(
            conversationOf(call),
            [question(sentQuestion(api), "Why is the sky blue?")],
            turn({ status: "running", finish_reason: null }),
          ),
        ),
      )
      // Read along with, and still being written.
      .on(STREAM, heldAfter("answer.sse", 6));
    const user = open();

    await ask(user, "Why is the sky blue?");

    expect(await screen.findByText(/^Echo: Why is/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();
    expect(screen.queryByText(UNREACHABLE)).toBeNull();

    // The page goes elsewhere, which ends the reading: the answer is written on.
    await user.click(screen.getByRole("button", { name: "New chat" }));
    await screen.findByText("What would you like to know?");
  });

  it("is looked for after this page's own connection broke off, and the break is still said when it can't be read along with", async () => {
    const api = fakeApi();
    api
      .on(TURNS, () => brokenAfter("answer.sse", 5))
      .on(CONVERSATION, (call) =>
        json(
          200,
          conversation(
            conversationOf(call),
            [question(sentQuestion(api), "Why is the sky blue?")],
            turn({ status: "running", finish_reason: null }),
          ),
        ),
      )
      .on(STREAM, () =>
        problem(503, "This can't be done right now. Try again in a moment."),
      );
    const user = open();

    await ask(user, "Why is the sky blue?");

    expect(
      await screen.findByText(/An answer to this is still being written/),
    ).toBeInTheDocument();
    expect(screen.getByText(UNREACHABLE)).toBeInTheDocument();
    expect(api.to(STREAM)).toHaveLength(1);
  });

  it("keeps saying that a Stop couldn't be sent, while it reads along with the answer that went on", async () => {
    const api = fakeApi();
    api
      .on(TURNS, heldAfter("answer.sse", 5))
      .on(CONVERSATION, (call) =>
        json(
          200,
          conversation(
            conversationOf(call),
            [question(sentQuestion(api), "Why is the sky blue?")],
            turn({ status: "running", finish_reason: null }),
          ),
        ),
      )
      // The connection went away just then: the stop never reached the server.
      .on(CANCEL, () => {
        throw new TypeError("fetch failed");
      })
      .on(STREAM, () => streamed("answer.sse"));
    const user = open();
    await ask(user, "Why is the sky blue?");
    await screen.findByText(/^Echo:/);

    await user.click(screen.getByRole("button", { name: "Stop" }));

    expect(
      await screen.findByText("Echo: Why is the sky blue?"),
    ).toBeInTheDocument();
    expect(screen.getByText(UNREACHABLE)).toBeInTheDocument();
    expect(api.to(CANCEL)).toHaveLength(1);
    expect(api.to(STREAM)).toHaveLength(1);
  });

  it("keeps saying that a question didn't reach the server, while it reads along with one asked elsewhere", async () => {
    const api = fakeApi()
      .on(
        CONVERSATION,
        () => json(200, written("Because of scattering.")),
        // Meanwhile another window asked, and its answer is being written.
        () =>
          json(
            200,
            conversation(
              RECORDED.conversation,
              [
                question(RECORDED.question, "Why is the sky blue?"),
                answer(
                  RECORDED.answer,
                  "Because of scattering.",
                  RECORDED.question,
                ),
                question("q-other", "Why is grass green?", RECORDED.answer),
              ],
              turn({ status: "running", finish_reason: null }),
            ),
          ),
      )
      // This window's question never got to the server.
      .on(TURNS, () => {
        throw new TypeError("fetch failed");
      })
      .on(STREAM, () => streamed("answer.sse", SECOND_ANSWER));
    const user = open(`/c/${RECORDED.conversation}`);
    await screen.findByText("Because of scattering.");

    await ask(user, "And at sunset?");

    expect(
      await screen.findByText("Echo: Why is the sky blue?"),
    ).toBeInTheDocument();
    expect(screen.getByText("Why is grass green?")).toBeInTheDocument();
    expect(screen.getByLabelText("Message")).toHaveValue("And at sunset?");
    expect(screen.getByText(UNREACHABLE)).toBeInTheDocument();
    expect(api.to(STREAM)).toHaveLength(1);
  });

  it("is left alone when its conversation is left while the page asks to read along", async () => {
    const reading = later(heldAfter("answer.sse", 5));
    const api = fakeApi()
      .on(CONVERSATION, () => json(200, beingWritten()))
      .on(STREAM, reading.answer);
    const user = open(`/c/${RECORDED.conversation}`);
    await screen.findByText("Waiting for the model…");

    await user.click(screen.getByRole("button", { name: "New chat" }));
    await screen.findByText("What would you like to know?");
    // The stream arrives after the page went on: it is not read, and starts nothing.
    reading.release();
    await afterAMoment();

    expect(api.to(STREAM)[0]?.signal?.aborted).toBe(true);
    expect(api.to(CONVERSATION)).toHaveLength(1);
    expect(screen.queryByRole("article")).toBeNull();
  });

  it("is read along with once React has run the page's effects twice, as it does in development", async () => {
    const api = fakeApi()
      .on(CONVERSATION, () => json(200, beingWritten()))
      .on(STREAM, () => streamed("answer.sse"));
    open(`/c/${RECORDED.conversation}`, { strict: true });

    expect(
      await screen.findByText("Echo: Why is the sky blue?"),
    ).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Send" })).toBeVisible();
    expect(screen.queryByText(/still being written/)).not.toBeInTheDocument();
    // The first request was given up when React undid the effects; the second read.
    const [dropped, read, ...more] = api.to(STREAM);
    expect(dropped?.signal?.aborted).toBe(true);
    expect(read?.signal?.aborted).toBe(false);
    expect(more).toEqual([]);
    // Read when it was loaded, which StrictMode does twice too, and not since.
    expect(api.to(CONVERSATION)).toHaveLength(2);
  });

  it("is forgotten when a question is asked before the server said how it stands", async () => {
    // The stream of the answer was lost, and the page asks the server how it stands.
    // That takes a while: meanwhile, a question is asked.
    let tell: () => void = () => undefined;
    const serverTold = new Promise<void>((resolve) => {
      tell = resolve;
    });
    const api = fakeApi()
      .on(
        CONVERSATION,
        () => json(200, beingWritten()),
        async () => {
          await serverTold;
          return json(200, beingWritten());
        },
      )
      .on(STREAM, () => streamed("lost.sse"))
      .on(TURNS, () => streamed("cut_off.sse", SECOND_ANSWER));
    const user = open(`/c/${RECORDED.conversation}`);
    await waitFor(() => {
      expect(api.to(CONVERSATION)).toHaveLength(2); // asked, and not yet told
    });

    await ask(user, "Count to five.");
    await screen.findByText("One, two, three");
    // What the server says about the earlier state arrives late, and changes nothing.
    tell();
    await afterAMoment();

    expect(screen.getByText("One, two, three")).toBeInTheDocument();
    expect(screen.queryByText(/still being written/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument();
    expect(api.to(STREAM)).toHaveLength(1);
    expect(api.to(CONVERSATION)).toHaveLength(2);
  });
});

describe("moving between conversations", () => {
  it("starts an empty one under a new id with New chat", async () => {
    const api = fakeApi().on(
      TURNS,
      () => streamed("answer.sse"),
      () => streamed("cut_off.sse", SECOND_ANSWER),
    );
    const user = open();
    await ask(user, "Why is the sky blue?");
    await screen.findByText("Echo: Why is the sky blue?");

    await user.click(screen.getByRole("button", { name: "New chat" }));

    expect(window.location.pathname).toBe("/");
    expect(screen.queryByRole("article")).not.toBeInTheDocument();

    await ask(user, "Count to five.");
    await screen.findByText("One, two, three");

    const [first, second] = api.to(TURNS);
    expect(conversationOf(second)).not.toBe(conversationOf(first));
    expect(posted(second).parent_id).toBeNull();
  });

  it("shows a conversation as it is now when coming back to it, not as it was loaded", async () => {
    const before = JSON.parse(recorded("conversation.json")) as {
      messages: unknown[];
    };
    const api = fakeApi()
      .on(
        CONVERSATION,
        () => json(200, before),
        // Loaded again later, it has the question that was asked meanwhile.
        () =>
          json(200, {
            ...before,
            messages: [
              ...before.messages,
              question("q2", "Count to five.", RECORDED.answer),
              answer(SECOND_ANSWER, "One, two, three", "q2"),
            ],
          }),
      )
      .on(TURNS, () => streamed("cut_off.sse", SECOND_ANSWER));
    const user = open(`/c/${RECORDED.conversation}`);
    await screen.findByText("Echo: Why is the sky blue?");
    await ask(user, "Count to five.");
    await screen.findByText("One, two, three");
    await user.click(screen.getByRole("button", { name: "New chat" }));
    expect(screen.queryByRole("article")).not.toBeInTheDocument();

    window.history.back();

    expect(await screen.findByText("One, two, three")).toBeInTheDocument();
    expect(screen.getAllByRole("article")).toHaveLength(4);
    expect(api.to(CONVERSATION)).toHaveLength(2);
  });

  it("goes back to the earlier one with the browser's Back", async () => {
    const api = fakeApi();
    api
      .on(TURNS, () => streamed("answer.sse"))
      .on(CONVERSATION, (call) =>
        json(
          200,
          conversation(conversationOf(call), [
            question(sentQuestion(api), "Why is the sky blue?"),
            answer(
              RECORDED.answer,
              "Echo: Why is the sky blue?",
              sentQuestion(api),
            ),
          ]),
        ),
      );
    const user = open();
    await ask(user, "Why is the sky blue?");
    await screen.findByText("Echo: Why is the sky blue?");
    const address = window.location.pathname;
    await user.click(screen.getByRole("button", { name: "New chat" }));
    expect(screen.queryByRole("article")).not.toBeInTheDocument();

    window.history.back();

    expect(
      await screen.findByText("Echo: Why is the sky blue?"),
    ).toBeInTheDocument();
    expect(window.location.pathname).toBe(address);
    // This time it came from the server.
    expect(api.to(CONVERSATION)).toHaveLength(1);
  });

  it("loads a conversation that was started here, once the page has left it", async () => {
    const api = fakeApi();
    api
      .on(TURNS, () => streamed("cut_off.sse", SECOND_ANSWER))
      .on(CONVERSATION, (call) =>
        conversationOf(call) === RECORDED.conversation
          ? new Response(recorded("conversation.json"), {
              headers: { "content-type": "application/json" },
            })
          : json(
              200,
              conversation(conversationOf(call), [
                question(sentQuestion(api), "Count to five."),
                answer(SECOND_ANSWER, "One, two, three", sentQuestion(api)),
              ]),
            ),
      );
    const user = open(`/c/${RECORDED.conversation}`);
    await screen.findByText("Echo: Why is the sky blue?");
    await user.click(screen.getByRole("button", { name: "New chat" }));
    await ask(user, "Count to five.");
    await screen.findByText("One, two, three");
    const started = window.location.pathname;

    window.history.back();
    await screen.findByText("Echo: Why is the sky blue?");
    window.history.forward();

    // The page let go of it when it showed the other one. It is read again, not
    // shown as an empty conversation under its address.
    expect(await screen.findByText("One, two, three")).toBeInTheDocument();
    expect(window.location.pathname).toBe(started);
    expect(screen.getAllByRole("article")).toHaveLength(2);
    expect(api.to(CONVERSATION).map((call) => conversationOf(call))).toEqual([
      RECORDED.conversation,
      RECORDED.conversation,
      started.slice(3),
    ]);
  });
});
