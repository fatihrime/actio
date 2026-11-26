// A question refused for now, driven as a person would: no model that could answer it
// has budget left for it, by Actio's count of what its models' providers take (ADR
// 0030), and the page counts the wait down above the box until it is over (ADR 0021,
// revision "a refusal for now is counted down").
//
// Against the fake API (tests/fake-api.ts). The refusal is the one the API was recorded
// giving (`no_budget.json`): a wait of 60 seconds, for the model "standard". The clock
// is set, and timers run as ever: a test moves the clock, and waits for the page to look
// at it, which it does every second.
import { act, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { clockAt, MINUTE, SECOND } from "@/tests/clock";
import {
  conversation,
  fakeApi,
  json,
  later,
  problem,
  question,
  refused,
  streamed,
  turn,
} from "@/tests/fake-api";
import type { Call, FakeApi } from "@/tests/fake-api";
import { ask, open } from "@/tests/open";

const TURNS = "POST /api/conversations/*/turns";
const CONVERSATION = "GET /api/conversations/*";
const NO_BUDGET = "no_budget.json";
const QUESTION = "Why is the sky blue?";

/** What the page says first, as a status. */
const REFUSED = "No model can take this message right now.";
/** What the API says of the same refusal, which the page doesn't. */
const API_WORDS = "Try again in 60 seconds.";
/** How long the page may take to notice that the clock was moved: it looks once a
 * second. */
const SOON = { timeout: 3000 };

/** What the API says of a conversation whose first question it refused. */
const missing = () => problem(404, "No such conversation.");

function box(): HTMLElement {
  return screen.getByLabelText("Message");
}

function send(): HTMLElement {
  return screen.getByRole("button", { name: "Send" });
}

function timer(): HTMLElement {
  return screen.getByRole("timer");
}

function dismiss(): HTMLElement {
  return screen.getByRole("button", { name: "Dismiss" });
}

/** The refusal for now, waiting `seconds`. */
function noBudget(seconds: number): Response {
  return refused(NO_BUDGET, { retry_after: seconds });
}

/** Wait until the words of a refused question are back in the box. */
async function backInTheBox(words = QUESTION): Promise<void> {
  await waitFor(() => {
    expect(box()).toHaveValue(words);
  });
}

/** Wait until the page has said the refusal, and the question is back in the box. */
async function refusedForNow(): Promise<void> {
  expect(await screen.findByText(REFUSED)).toBeInTheDocument();
  await backInTheBox();
}

/** Wait until the page has taken the notice away. */
async function gone(): Promise<void> {
  await waitFor(() => {
    expect(screen.queryByText(REFUSED)).not.toBeInTheDocument();
  }, SOON);
}

/** The conversation id in a call's path. */
function conversationOf(call: Call | undefined): string {
  return call?.path.split("/")[3] ?? "";
}

/** The id of the question the page sent in its first turn. */
function sentQuestion(api: FakeApi): string {
  const body = api.to(TURNS)[0]?.body as { message?: { id: string } };
  return body.message?.id ?? "";
}

describe("a question refused for now", () => {
  it("is said in the page's words, with the time left as a timer beside the status, and not in the API's", async () => {
    clockAt();
    const api = fakeApi()
      .on(TURNS, () => refused(NO_BUDGET))
      .on(CONVERSATION, missing);
    const user = open();

    await ask(user, QUESTION);

    const status = await screen.findByText(REFUSED);
    expect(status).toHaveAttribute("role", "status");
    // The time left is a timer, which changes without being said again: beside the
    // status, never inside one.
    expect(timer()).toHaveTextContent(/^in 1 minute$/);
    expect(timer().closest('[role="status"]')).toBeNull();
    expect(status).not.toContainElement(timer());
    expect(timer().parentElement).toHaveTextContent(
      /^This installation has reached the limits of its models’ providers\. Try again in 1 minute\.$/,
    );
    // The API's "in 60 seconds" would stand still beside it.
    expect(document.body).not.toHaveTextContent(API_WORDS);
    expect(dismiss()).toBeInTheDocument();
    // The question was never asked: its words are back in the box, and it can be sent
    // again at once.
    await backInTheBox();
    expect(send()).toBeEnabled();
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
    expect(api.to(TURNS)).toHaveLength(1);
  });

  it("counts the wait down", async () => {
    const clock = clockAt();
    fakeApi()
      .on(TURNS, () => refused(NO_BUDGET))
      .on(CONVERSATION, missing);
    const user = open();
    await ask(user, QUESTION);
    await refusedForNow();

    clock.advance(20 * SECOND);

    await waitFor(() => {
      expect(timer()).toHaveTextContent(/^in 40 seconds$/);
    }, SOON);
  });

  it("goes by itself when the wait is over, and sends nothing until its writer does", async () => {
    const clock = clockAt();
    const api = fakeApi()
      .on(
        TURNS,
        () => refused(NO_BUDGET),
        () => streamed("answer.sse"),
      )
      .on(CONVERSATION, missing);
    const user = open();
    await ask(user, QUESTION);
    await refusedForNow();

    clock.advance(61 * SECOND);

    await gone();
    // Nothing else is said in its place: not the API's words either.
    expect(screen.queryByRole("timer")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Dismiss" }),
    ).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(API_WORDS);
    // The question waits in the box for its writer.
    expect(box()).toHaveValue(QUESTION);
    expect(api.to(TURNS)).toHaveLength(1);

    await user.click(send());

    expect(await screen.findByText(`Echo: ${QUESTION}`)).toBeInTheDocument();
    expect(api.to(TURNS)).toHaveLength(2);
  });

  it("keeps no question from being sent meanwhile, and goes when one is", async () => {
    clockAt();
    const answer = later(() => streamed("answer.sse"));
    const api = fakeApi()
      .on(TURNS, () => refused(NO_BUDGET), answer.answer)
      .on(CONVERSATION, missing);
    const user = open();
    await ask(user, QUESTION);
    await refusedForNow();

    // A shorter question may be taken sooner: nothing keeps one from being sent.
    expect(send()).toBeEnabled();
    expect(send()).not.toHaveAttribute("title");
    await user.click(send());

    // Gone from the moment it is sent, while the question is still on its way.
    expect(screen.queryByText(REFUSED)).not.toBeInTheDocument();
    expect(screen.queryByRole("timer")).not.toBeInTheDocument();
    expect(api.to(TURNS)).toHaveLength(2);
    // And not back once the question is answered, with the whole of the first wait
    // still to go: the clock hasn't moved.
    answer.release();
    expect(await screen.findByText(`Echo: ${QUESTION}`)).toBeInTheDocument();
    await waitFor(() => {
      expect(send()).toBeVisible();
    });
    expect(screen.queryByText(REFUSED)).not.toBeInTheDocument();
    expect(screen.queryByRole("timer")).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(API_WORDS);
  });

  it.each([
    { first: 60, then: 15, says: /^in 15 seconds$/ },
    { first: 15, then: 60, says: /^in 1 minute$/ },
  ])(
    "takes a second refusal's wait for the first's: $first s, then $then s",
    async ({ first, then, says }) => {
      const clock = clockAt();
      fakeApi()
        .on(
          TURNS,
          () => noBudget(first),
          () => {
            // Ten seconds after the first: a wait counted from the first one's
            // arrival would say ten seconds less.
            clock.advance(10 * SECOND);
            return noBudget(then);
          },
        )
        .on(CONVERSATION, missing);
      const user = open();
      await ask(user, QUESTION);
      await refusedForNow();

      await user.click(send());

      // One notice, with the newer wait, counted from its own arrival.
      await waitFor(() => {
        expect(timer()).toHaveTextContent(says);
      });
      expect(screen.getAllByText(REFUSED)).toHaveLength(1);
      expect(screen.getAllByRole("timer")).toHaveLength(1);
    },
  );

  it("isn't taken away by the end of a wait that a newer one replaced", async () => {
    const clock = clockAt();
    fakeApi()
      .on(
        TURNS,
        () => noBudget(15),
        () => {
          clock.advance(10 * SECOND);
          return noBudget(60);
        },
      )
      .on(CONVERSATION, missing);
    const user = open();
    await ask(user, QUESTION);
    await refusedForNow();
    await user.click(send());
    await waitFor(() => {
      expect(timer()).toHaveTextContent(/^in 1 minute$/);
    });
    await backInTheBox();

    // Past the first wait's end, and not the second's.
    clock.advance(20 * SECOND);

    // The page has looked: its timer says so. And a second later it has looked once
    // more, and so has whatever else looks at the clock, on a beat of its own.
    await waitFor(() => {
      expect(timer()).toHaveTextContent(/^in 40 seconds$/);
    }, SOON);
    clock.advance(1 * SECOND);
    await waitFor(() => {
      expect(timer()).toHaveTextContent(/^in 39 seconds$/);
    }, SOON);
    expect(screen.getByText(REFUSED)).toBeInTheDocument();
  });

  it("counts the wait from the moment the refusal arrived", async () => {
    const clock = clockAt();
    fakeApi()
      .on(TURNS, () => {
        // Half a minute between the question being sent and the refusal arriving.
        clock.advance(30 * SECOND);
        return refused(NO_BUDGET);
      })
      .on(CONVERSATION, missing);
    const user = open();
    await screen.findByLabelText("Message");
    // Minutes between the page being opened and the question being asked.
    clock.advance(5 * MINUTE);

    await ask(user, QUESTION);

    await screen.findByText(REFUSED);
    expect(timer()).toHaveTextContent(/^in 1 minute$/);
  });

  it.each([
    { seconds: 86_400, says: /^in 24 hours$/ },
    { seconds: 200_000, says: /^in 2 days 8 hours$/ },
  ])(
    "says a wait of $seconds s in hours and days",
    async ({ seconds, says }) => {
      clockAt();
      fakeApi()
        .on(TURNS, () => noBudget(seconds))
        .on(CONVERSATION, missing);
      const user = open();

      await ask(user, QUESTION);

      await screen.findByText(REFUSED);
      expect(timer()).toHaveTextContent(says);
    },
  );

  it("says a wait of one second, and goes at the next look after it", async () => {
    const clock = clockAt();
    fakeApi()
      .on(TURNS, () => noBudget(1))
      .on(CONVERSATION, missing);
    const user = open();
    await ask(user, QUESTION);

    await screen.findByText(REFUSED);
    expect(timer()).toHaveTextContent(/^in 1 second$/);

    clock.advance(1 * SECOND);

    await gone();
    expect(screen.queryByRole("timer")).not.toBeInTheDocument();
  });

  it("is taken away by Dismiss, and the keyboard goes to the question in the box", async () => {
    clockAt();
    fakeApi()
      .on(TURNS, () => refused(NO_BUDGET))
      .on(CONVERSATION, missing);
    const user = open();
    await ask(user, QUESTION);
    await refusedForNow();

    await user.click(dismiss());

    expect(screen.queryByText(REFUSED)).not.toBeInTheDocument();
    expect(screen.queryByRole("timer")).not.toBeInTheDocument();
    expect(box()).toHaveValue(QUESTION);
    expect(box()).toHaveFocus();
  });

  it("puts the keyboard on the box when it goes by itself while the keyboard is on its button", async () => {
    const clock = clockAt();
    fakeApi()
      .on(TURNS, () => refused(NO_BUDGET))
      .on(CONVERSATION, missing);
    const user = open();
    await ask(user, QUESTION);
    await refusedForNow();
    act(() => {
      dismiss().focus();
    });
    expect(dismiss()).toHaveFocus();

    clock.advance(61 * SECOND);

    await gone();
    expect(box()).toHaveFocus();
  });

  it.each([
    { on: "the box", element: box },
    { on: "Send", element: send },
  ])(
    "leaves the keyboard on $on when it goes by itself",
    async ({ element }) => {
      const clock = clockAt();
      fakeApi()
        .on(TURNS, () => refused(NO_BUDGET))
        .on(CONVERSATION, missing);
      const user = open();
      await ask(user, QUESTION);
      await refusedForNow();
      act(() => {
        element().focus();
      });
      expect(element()).toHaveFocus();

      clock.advance(61 * SECOND);

      await gone();
      expect(element()).toHaveFocus();
    },
  );

  it("is kept by its conversation alone", async () => {
    clockAt();
    fakeApi()
      .on(TURNS, () => refused(NO_BUDGET))
      .on(CONVERSATION, missing);
    const user = open();
    await ask(user, QUESTION);
    await refusedForNow();

    await user.click(screen.getByRole("button", { name: "New chat" }));
    await user.type(box(), "And in another conversation?");

    expect(screen.queryByText(REFUSED)).not.toBeInTheDocument();
    expect(screen.queryByRole("timer")).not.toBeInTheDocument();
    expect(send()).toBeEnabled();
  });

  it("counts down another try that is refused, with the question still in the conversation", async () => {
    const clock = clockAt();
    const api = fakeApi();
    api
      .on(
        TURNS,
        () => streamed("model_unavailable.sse"),
        () => refused(NO_BUDGET),
      )
      .on(CONVERSATION, (call) =>
        json(
          200,
          conversation(
            conversationOf(call),
            [question(sentQuestion(api), QUESTION)],
            turn({ status: "failed", error: "model_unavailable" }),
          ),
        ),
      );
    const user = open();
    await ask(user, QUESTION);
    await screen.findByText(/can't be reached, or is too busy/);

    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByText(REFUSED)).toBeInTheDocument();
    expect(timer()).toHaveTextContent(/^in 1 minute$/);
    expect(api.to(TURNS)).toHaveLength(2);
    clock.advance(20 * SECOND);
    await waitFor(() => {
      expect(timer()).toHaveTextContent(/^in 40 seconds$/);
    }, SOON);
    // The question is the conversation's, as it was, and can be tried again.
    expect(screen.getAllByRole("article")).toHaveLength(1);
    expect(screen.getByRole("article", { name: "You" })).toHaveTextContent(
      QUESTION,
    );
    expect(screen.getByRole("button", { name: "Try again" })).toBeEnabled();
  });

  it("says one notice in StrictMode, which counts down and goes", async () => {
    const clock = clockAt();
    fakeApi()
      .on(TURNS, () => refused(NO_BUDGET))
      .on(CONVERSATION, missing);
    const user = open("/", { strict: true });
    await ask(user, QUESTION);
    await refusedForNow();
    expect(screen.getAllByText(REFUSED)).toHaveLength(1);
    expect(screen.getAllByRole("timer")).toHaveLength(1);

    clock.advance(20 * SECOND);
    await waitFor(() => {
      expect(timer()).toHaveTextContent(/^in 40 seconds$/);
    }, SOON);
    clock.advance(41 * SECOND);

    await gone();
    expect(screen.queryByRole("timer")).not.toBeInTheDocument();
  });
});

describe("a refusal that isn't for now", () => {
  it.each([
    {
      what: "too many messages",
      refusal: () =>
        problem(
          429,
          "Too many messages in a short time. Try again in 40 seconds.",
          { retry_after: 40 },
        ),
      said: "Too many messages in a short time. Try again in 40 seconds.",
    },
    {
      what: "too many answers at once",
      refusal: () =>
        problem(
          429,
          "2 answers are already being written for you, as many as your plan allows at once. Wait for one to finish, or stop one.",
          { at_once: 2 },
        ),
      said: "2 answers are already being written for you, as many as your plan allows at once. Wait for one to finish, or stop one.",
    },
    {
      what: "a 429 that names a model and no wait",
      refusal: () => refused(NO_BUDGET, { retry_after: undefined }),
      said: "No model can take this message right now: this installation has reached the limits of its models' providers. Try again in 60 seconds.",
    },
    {
      what: "a 429 that names a model, a wait, and a window that is no usage window's",
      refusal: () => refused(NO_BUDGET, { window: "month" }),
      said: "No model can take this message right now: this installation has reached the limits of its models' providers. Try again in 60 seconds.",
    },
    {
      what: "a 503 that names a model and a wait",
      refusal: () =>
        problem(503, "This model's server can't be reached.", {
          model: "standard",
          model_status: "unreachable",
          retry_after: 30,
        }),
      said: "This model's server can't be reached.",
    },
  ])("is said in the API's words: $what", async ({ refusal, said }) => {
    clockAt();
    fakeApi().on(TURNS, refusal).on(CONVERSATION, missing);
    const user = open();

    await ask(user, QUESTION);

    expect(await screen.findByText(said)).toBeInTheDocument();
    await backInTheBox();
    expect(screen.queryByText(REFUSED)).not.toBeInTheDocument();
    expect(screen.queryByRole("timer")).not.toBeInTheDocument();
    expect(dismiss()).toBeInTheDocument();
    expect(send()).toBeEnabled();
  });

  it("is a usage limit when it names a usage window, also with a model", async () => {
    clockAt();
    const api = fakeApi()
      .on(TURNS, () => refused("limit_reached.json", { model: "standard" }))
      .on(CONVERSATION, missing);
    const user = open();

    await ask(user, QUESTION);

    expect(
      await screen.findByText(
        "You’ve reached your usage limit for this session.",
      ),
    ).toBeInTheDocument();
    await backInTheBox();
    expect(screen.queryByText(REFUSED)).not.toBeInTheDocument();
    expect(send()).toBeDisabled();
    await user.type(box(), "{Enter}");
    expect(api.to(TURNS)).toHaveLength(1);
  });
});
