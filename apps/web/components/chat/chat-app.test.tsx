// The chat, driven as a person would: through the page, against a fake API that
// answers with the API's recorded streams (tests/fake-api.ts).
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { UserEvent } from "@testing-library/user-event";
import { SWRConfig } from "swr";
import { describe, expect, it } from "vitest";

import { isId } from "@/lib/ids";
import {
  answer,
  conversation,
  fakeApi,
  GUEST,
  heldAfter,
  json,
  models,
  problem,
  question,
  streamed,
  turn,
} from "@/tests/fake-api";
import type { Call, FakeApi } from "@/tests/fake-api";
import { recorded, RECORDED } from "@/tests/recorded";

import { ChatApp } from "./chat-app";

const TURNS = "POST /api/conversations/*/turns";
const CONVERSATION = "GET /api/conversations/*";
const SECOND_ANSWER = "01900000-0000-7000-8000-000000000004";

/** Open the app at `path`, as a browser with nothing remembered. */
function open(path = "/"): UserEvent {
  window.history.replaceState(null, "", path);
  render(
    // Its own cache, so one test's session isn't the next one's.
    <SWRConfig value={{ provider: () => new Map() }}>
      <ChatApp />
    </SWRConfig>,
  );
  return userEvent.setup();
}

async function ask(user: UserEvent, text: string): Promise<void> {
  await user.type(await screen.findByLabelText("Message"), text);
  await user.click(screen.getByRole("button", { name: "Send" }));
}

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

describe("a first visit", () => {
  it("starts a guest session, and shows where to write", async () => {
    const api = fakeApi();

    open();

    expect(await screen.findByLabelText("Message")).toBeInTheDocument();
    expect(api.to("POST /api/session/guest")).toHaveLength(1);
    expect(screen.getByText(/Answered by Qwen 3.5 4B/)).toBeInTheDocument();
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
      screen.getAllByRole("listitem").map((item) => item.textContent),
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
        // Asked right after the stop, the server hasn't recorded it yet.
        (call) =>
          json(200, kept(call, { status: "running", finish_reason: null })),
        (call) =>
          json(
            200,
            kept(call, { status: "stopped", finish_reason: null }, "Echo: Why"),
          ),
      );
    const user = open();

    await ask(user, "Why is the sky blue?");
    await screen.findByText(/^Echo:/);
    await user.click(screen.getByRole("button", { name: "Stop" }));

    expect(api.to(TURNS)[0]?.signal?.aborted).toBe(true);
    expect(
      await screen.findByText(
        "This answer was stopped before it was finished.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Echo: Why")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument();
    expect(api.to(CONVERSATION)).toHaveLength(2);
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

  it("waits for an answer that another window is writing", async () => {
    const api = fakeApi().on(
      CONVERSATION,
      () =>
        json(
          200,
          conversation(
            RECORDED.conversation,
            [question(RECORDED.question, "Why is the sky blue?")],
            turn({ status: "running", finish_reason: null }),
          ),
        ),
      () =>
        json(
          200,
          conversation(RECORDED.conversation, [
            question(RECORDED.question, "Why is the sky blue?"),
            answer(
              RECORDED.answer,
              "Because of scattering.",
              RECORDED.question,
            ),
          ]),
        ),
    );
    const user = open(`/c/${RECORDED.conversation}`);

    expect(
      await screen.findByText(/being written in another window/),
    ).toBeInTheDocument();
    await user.type(screen.getByLabelText("Message"), "And at sunset?");
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();

    expect(
      await screen.findByText("Because of scattering.", {}, { timeout: 4000 }),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/being written in another window/),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send" })).toBeEnabled();
    expect(api.to(CONVERSATION)).toHaveLength(2);
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
});
