// The usage limits in the page, driven as a person would: the meter under the list of
// conversations, what happens when a limit is reached, the model that costs nothing,
// and the warnings on the way there.
//
// Against the fake API (tests/fake-api.ts). A question at a limit is answered with the
// refusal the API was recorded giving (`limit_reached.json`): reached in a session
// that starts over five hours after noon, with one model, `local`, that costs nothing.
import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { clockAt, MINUTE, SECOND } from "@/tests/clock";
import {
  account,
  fakeApi,
  freeModel,
  GUEST,
  json,
  model,
  problem,
  refused,
  streamed,
  usage,
  usageWindow,
} from "@/tests/fake-api";
import { ask, open } from "@/tests/open";

const TURNS = "POST /api/conversations/*/turns";
const CONVERSATION = "GET /api/conversations/*";
const USAGE = "GET /api/usage";
const MODELS = "GET /api/models";
const LIMIT = "limit_reached.json";
const SECOND_ANSWER = "01900000-0000-7000-8000-000000000004";

const REACHED = "You’ve reached your usage limit for this session.";
/** How long the page may take to notice that the clock was moved: it looks at least
 * once a second. */
const SOON = { timeout: 3000 };

/** What the API says of a conversation whose first question it refused. */
const missing = () => problem(404, "No such conversation.");
/** An installation that offers a second model, which costs nothing. */
const withFreeModel = () =>
  json(200, { default: "standard", models: [model(), freeModel()] });

function meter(name: string): HTMLElement {
  return screen.getByRole("meter", { name });
}

function box(): HTMLElement {
  return screen.getByLabelText("Message");
}

function send(): HTMLElement {
  return screen.getByRole("button", { name: "Send" });
}

/** Wait until the words of a refused question are back in the box. */
async function backInTheBox(words: string): Promise<void> {
  await waitFor(() => {
    expect(box()).toHaveValue(words);
  });
}

describe("the usage meter", () => {
  it("shows how much of each limit is used, and when it resets", async () => {
    clockAt();
    fakeApi().on(USAGE, () =>
      json(
        200,
        usage([
          usageWindow("session", {
            used: 10_500,
            resets_at: "2026-11-10T14:30:00Z",
          }),
          usageWindow("week", { used: 12_999 }),
        ]),
      ),
    );

    open();

    const region = await screen.findByRole("region", { name: "Usage" });
    expect(within(region).getByText("Guest plan")).toBeInTheDocument();
    expect(meter("Session")).toHaveAttribute("aria-valuenow", "42");
    expect(meter("Session")).toHaveAttribute("aria-valuetext", "42% used");
    expect(within(region).getByText("42% used")).toBeInTheDocument();
    expect(
      within(region).getByText(/^Resets at 2:30\sPM$/),
    ).toBeInTheDocument();
    // Rounded down: a limit isn't nearer than it is.
    expect(meter("Week")).toHaveAttribute("aria-valuetext", "12% used");
    expect(
      within(region).getByText(/^Resets on Mon, Nov 16, 12:00\sAM$/),
    ).toBeInTheDocument();
  });

  it("says that a session starts with the next question", async () => {
    fakeApi();

    open();

    const region = await screen.findByRole("region", { name: "Usage" });
    expect(meter("Session")).toHaveAttribute("aria-valuetext", "0% used");
    expect(
      within(region).getByText("Starts with your next question"),
    ).toBeInTheDocument();
  });

  it("doesn't call a little nothing", async () => {
    fakeApi().on(USAGE, () =>
      json(200, usage([usageWindow("session", { used: 118 })])),
    );

    open();

    expect(await screen.findByText("under 1% used")).toBeInTheDocument();
    expect(meter("Session")).toHaveAttribute("aria-valuenow", "0");
  });

  it("is read again when an answer has ended", async () => {
    const api = fakeApi()
      .on(TURNS, () => streamed("answer.sse"))
      .on(
        USAGE,
        () => json(200, usage()),
        () =>
          json(
            200,
            usage([
              usageWindow("session", {
                used: 2_500,
                resets_at: "2026-11-10T17:00:00Z",
              }),
              usageWindow("week", { used: 2_500 }),
            ]),
          ),
      );
    const user = open();
    await screen.findByRole("region", { name: "Usage" });

    await ask(user, "Why is the sky blue?");
    await screen.findByText("Echo: Why is the sky blue?");

    await waitFor(() => {
      expect(meter("Session")).toHaveAttribute("aria-valuetext", "10% used");
    });
    expect(meter("Week")).toHaveAttribute("aria-valuetext", "2% used");
    // Once to begin with, and once for the turn: not at every piece of the answer.
    expect(api.to(USAGE)).toHaveLength(2);
  });

  it("is read again when a window starts over", async () => {
    const clock = clockAt();
    fakeApi().on(
      USAGE,
      () =>
        json(
          200,
          usage([
            usageWindow("session", {
              used: 20_000,
              resets_at: "2026-11-10T12:00:01Z",
            }),
            usageWindow("week", { used: 20_000 }),
          ]),
        ),
      () => json(200, usage([usageWindow("session"), usageWindow("week")])),
    );
    open();
    await screen.findByRole("region", { name: "Usage" });
    expect(meter("Session")).toHaveAttribute("aria-valuetext", "80% used");

    clock.advance(2 * SECOND);

    await waitFor(() => {
      expect(meter("Session")).toHaveAttribute("aria-valuetext", "0% used");
    }, SOON);
    expect(
      screen.getByText("Starts with your next question"),
    ).toBeInTheDocument();
  });

  it("goes when the plan has no limits", async () => {
    fakeApi()
      .on(TURNS, () => streamed("answer.sse"))
      .on(
        USAGE,
        () => json(200, usage()),
        () => json(200, usage([])),
      );
    const user = open();
    await screen.findByRole("region", { name: "Usage" });

    await ask(user, "Why is the sky blue?");

    await waitFor(() => {
      expect(
        screen.queryByRole("region", { name: "Usage" }),
      ).not.toBeInTheDocument();
    });
  });

  it("says so quietly when the usage can't be read, and the chat goes on", async () => {
    fakeApi()
      .on(TURNS, () => streamed("answer.sse"))
      .on(USAGE, () =>
        problem(
          503,
          "Usage can't be counted right now. Try again in a moment.",
        ),
      );
    const user = open();

    expect(
      await screen.findByText("Your usage can’t be read right now."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("meter")).not.toBeInTheDocument();

    await ask(user, "Why is the sky blue?");

    expect(
      await screen.findByText("Echo: Why is the sky blue?"),
    ).toBeInTheDocument();
  });

  it("is read anew for the account a guest becomes", async () => {
    fakeApi()
      .on("POST /api/account", () => json(201, account({ id: GUEST.id })))
      .on(
        USAGE,
        () => json(200, usage()),
        () =>
          json(200, {
            plan: { id: "free", title: "Free" },
            windows: [
              usageWindow("session", { limit: 100_000 }),
              usageWindow("week", { limit: 600_000 }),
            ],
          }),
      );
    const user = open();
    await screen.findByText("Guest plan");

    await user.click(
      within(screen.getByRole("banner")).getByRole("link", { name: "Sign up" }),
    );
    await user.type(
      await screen.findByLabelText("Email address"),
      "ada@example.com",
    );
    await user.type(screen.getByLabelText("Password"), "correct horse battery");
    await user.click(
      within(screen.getByRole("main")).getByRole("button", { name: "Sign up" }),
    );

    // The same account, by its id: what was read for the guest's session is not
    // shown in the account's.
    expect(await screen.findByText("Free plan")).toBeInTheDocument();
    expect(screen.queryByText("Guest plan")).not.toBeInTheDocument();
  });
});

describe("a usage limit that is reached", () => {
  it("says which limit, when it resets and how long that is, and sends nothing meanwhile", async () => {
    clockAt();
    const api = fakeApi()
      .on(TURNS, () => refused(LIMIT, { alternatives: [] }))
      .on(CONVERSATION, missing);
    const user = open();

    await ask(user, "Why is the sky blue?");

    // Said once, as a status. The time left is a timer beside it, which changes
    // without being said again.
    expect(await screen.findByText(REACHED)).toHaveAttribute("role", "status");
    expect(screen.getByText(/It resets at 5:00\sPM,/)).toBeInTheDocument();
    expect(screen.getByRole("timer")).toHaveTextContent(/^in 5 hours$/);
    // The question was never asked: its words are back in the box, for later.
    await backInTheBox("Why is the sky blue?");
    expect(send()).toBeDisabled();
    expect(send()).toHaveAttribute("title", "You’ve reached your usage limit.");
    await user.type(box(), "{Enter}");
    expect(api.to(TURNS)).toHaveLength(1);
    // It isn't this question's problem alone: there is nothing to dismiss, and here
    // no other model to choose.
    expect(
      screen.queryByRole("button", { name: "Dismiss" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /^Use / }),
    ).not.toBeInTheDocument();
  });

  it("counts down", async () => {
    const clock = clockAt();
    fakeApi()
      .on(TURNS, () => refused(LIMIT))
      .on(CONVERSATION, missing);
    const user = open();
    await ask(user, "Why is the sky blue?");
    await screen.findByText(REACHED);

    clock.advance(61 * MINUTE);

    await waitFor(() => {
      expect(screen.getByRole("timer")).toHaveTextContent(
        /^in 3 hours 59 minutes$/,
      );
    }, SOON);
    expect(screen.getByText(/It resets at 5:00\sPM,/)).toBeInTheDocument();
  });

  it("is over when the limit has started over, and questions are taken again", async () => {
    const clock = clockAt();
    const api = fakeApi()
      .on(
        TURNS,
        // The session starts over in a second.
        () => refused(LIMIT, { retry_after: 1 }),
        () => streamed("answer.sse"),
      )
      .on(CONVERSATION, missing);
    const user = open();
    await ask(user, "Why is the sky blue?");
    await screen.findByText(REACHED);
    await backInTheBox("Why is the sky blue?");
    const read = api.to(USAGE).length;

    clock.advance(2 * SECOND);

    await waitFor(() => {
      expect(screen.queryByText(REACHED)).not.toBeInTheDocument();
    }, SOON);
    expect(api.to(USAGE).length).toBeGreaterThan(read); // the counts, read again
    await user.click(send());
    expect(
      await screen.findByText("Echo: Why is the sky blue?"),
    ).toBeInTheDocument();
  });

  it("holds for every conversation, not only the one that met it", async () => {
    clockAt();
    const api = fakeApi()
      .on(TURNS, () => refused(LIMIT))
      .on(CONVERSATION, missing);
    const user = open();
    await ask(user, "Why is the sky blue?");
    await screen.findByText(REACHED);
    await backInTheBox("Why is the sky blue?");

    await user.click(screen.getByRole("button", { name: "New chat" }));
    await user.type(box(), "And in another conversation?{Enter}");

    expect(screen.getByText(REACHED)).toBeInTheDocument();
    expect(send()).toBeDisabled();
    expect(api.to(TURNS)).toHaveLength(1);
  });

  it("is the session's: whoever logs in isn't held to the guest's limit", async () => {
    clockAt();
    fakeApi()
      .on(TURNS, () => refused(LIMIT))
      .on(CONVERSATION, missing)
      .on("POST /api/session", () => json(200, account()));
    const user = open();
    await ask(user, "Why is the sky blue?");
    await screen.findByText(REACHED);

    await user.click(
      within(screen.getByRole("banner")).getByRole("link", { name: "Log in" }),
    );
    await user.type(
      await screen.findByLabelText("Email address"),
      "ada@example.com",
    );
    await user.type(screen.getByLabelText("Password"), "correct horse battery");
    await user.click(
      within(screen.getByRole("main")).getByRole("button", { name: "Log in" }),
    );

    await user.type(await screen.findByLabelText("Message"), "Hello again");
    expect(screen.queryByText(REACHED)).not.toBeInTheDocument();
    expect(send()).toBeEnabled();
  });

  it("isn't taken for a question that is too large for the plan", async () => {
    const tooLarge =
      "This message alone needs more than your plan allows in a session.";
    fakeApi()
      .on(TURNS, () =>
        refused(LIMIT, {
          detail: tooLarge,
          retry_after: undefined,
          resets_at: null,
        }),
      )
      .on(CONVERSATION, missing);
    const user = open();

    await ask(user, "Why is the sky blue?");

    // No waiting helps this question. It is said, and the next one can be sent.
    expect(await screen.findByText(tooLarge)).toBeInTheDocument();
    expect(screen.queryByRole("timer")).not.toBeInTheDocument();
    await backInTheBox("Why is the sky blue?");
    expect(send()).toBeEnabled();
    expect(screen.getByRole("button", { name: "Dismiss" })).toBeInTheDocument();
  });

  it("isn't taken for as many answers being written as the plan allows at once", async () => {
    // What the API says then: no window, and no time to wait, since nobody knows when
    // an answer ends.
    const atOnce =
      "2 answers are already being written for you, as many as your plan allows at once. Wait for one to finish, or stop one.";
    const api = fakeApi()
      .on(TURNS, () => problem(429, atOnce, { at_once: 2 }))
      .on(CONVERSATION, missing);
    const user = open();

    await ask(user, "Why is the sky blue?");

    // In the API's words, as that question's own problem: once an answer ends, or is
    // stopped, it can be sent again.
    expect(await screen.findByText(atOnce)).toBeInTheDocument();
    await backInTheBox("Why is the sky blue?");
    expect(screen.queryByText(/usage limit/)).not.toBeInTheDocument();
    expect(screen.queryByRole("timer")).not.toBeInTheDocument();
    expect(send()).toBeEnabled();
    expect(screen.getByRole("button", { name: "Dismiss" })).toBeInTheDocument();
    expect(api.to(TURNS)).toHaveLength(1);
  });
});

describe("a model that costs nothing", () => {
  it("is offered at the limit, and answers from then on", async () => {
    clockAt();
    const api = fakeApi()
      .on(MODELS, withFreeModel)
      .on(
        TURNS,
        () => refused(LIMIT),
        () => streamed("answer.sse"),
        () => streamed("cut_off.sse", SECOND_ANSWER),
      )
      .on(CONVERSATION, missing);
    const user = open();
    await ask(user, "Why is the sky blue?");

    await user.click(
      await screen.findByRole("button", { name: "Use Local (free)" }),
    );

    // The keyboard is where the refused question waits to be sent.
    expect(box()).toHaveFocus();
    expect(screen.getByText("Local is answering, at no cost.")).toHaveAttribute(
      "role",
      "status",
    );
    expect(
      screen.getByText(
        /You’ve reached your usage limit for this session\. It resets at 5:00\sPM,/,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(/Answered by Local,/)).toBeInTheDocument();
    // The model that stands in can't reason, so the switch for it is gone.
    expect(
      screen.queryByLabelText("Think before answering"),
    ).not.toBeInTheDocument();
    await backInTheBox("Why is the sky blue?");

    await user.type(box(), "{Enter}");
    await screen.findByText("Echo: Why is the sky blue?");
    await ask(user, "Count to five.");
    await screen.findByText("One, two, three");

    const [first, second, third] = api.to(TURNS);
    expect(first?.body).not.toHaveProperty("model"); // the usual one: none named
    expect(second?.body).toMatchObject({ model: "local", thinking: false });
    expect(third?.body).toMatchObject({ model: "local" });
  });

  it("isn't offered when the installation no longer has it", async () => {
    clockAt();
    // The refusal names `local`, and the list of models doesn't.
    fakeApi()
      .on(TURNS, () => refused(LIMIT))
      .on(CONVERSATION, missing);
    const user = open();

    await ask(user, "Why is the sky blue?");

    await screen.findByText(REACHED);
    expect(
      screen.queryByRole("button", { name: /^Use / }),
    ).not.toBeInTheDocument();
    expect(send()).toBeDisabled();
  });

  it("gives way to the usual model when the limit has started over", async () => {
    const clock = clockAt();
    const api = fakeApi()
      .on(MODELS, withFreeModel)
      .on(
        TURNS,
        () => refused(LIMIT, { retry_after: 1 }),
        () => streamed("answer.sse"),
      )
      .on(CONVERSATION, missing);
    const user = open();
    await ask(user, "Why is the sky blue?");
    await user.click(
      await screen.findByRole("button", { name: "Use Local (free)" }),
    );
    await screen.findByText("Local is answering, at no cost.");
    await backInTheBox("Why is the sky blue?");

    clock.advance(2 * SECOND);

    await waitFor(() => {
      expect(
        screen.queryByText("Local is answering, at no cost."),
      ).not.toBeInTheDocument();
    }, SOON);
    expect(screen.getByText(/Answered by Qwen 3.5 4B,/)).toBeInTheDocument();
    await user.click(send());
    await screen.findByText("Echo: Why is the sky blue?");
    expect(api.to(TURNS)[1]?.body).not.toHaveProperty("model");
  });
});

describe("a share of a limit that is passed", () => {
  const halfUsed = () =>
    json(
      200,
      usage([
        usageWindow("session", {
          limit: 1000,
          used: 550,
          resets_at: "2026-11-10T17:00:00Z",
        }),
        usageWindow("week", { used: 550 }),
      ]),
    );

  it("is said once, with when the limit resets, until it is dismissed", async () => {
    clockAt();
    fakeApi()
      .on(TURNS, () => streamed("usage_warning.sse"))
      .on(USAGE, () => json(200, usage()), halfUsed);
    const user = open();

    await ask(user, "How much may I still ask?");

    const warning = await screen.findByText(
      /^You’ve used over 50% of your usage limit for this session\./,
    );
    // When that is, the counts say, which are read again when the turn has ended.
    await waitFor(() => {
      expect(warning).toHaveTextContent(/ It resets at 5:00\sPM\.$/);
    });
    expect(screen.getByText("That was half of it.")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Dismiss" }));

    expect(warning).not.toBeInTheDocument();
    expect(box()).toHaveFocus();
    expect(screen.getByText("That was half of it.")).toBeInTheDocument();
  });

  it("is put away when the next question is sent", async () => {
    fakeApi()
      .on(
        TURNS,
        () => streamed("usage_warning.sse"),
        () => streamed("cut_off.sse", SECOND_ANSWER),
      )
      .on(USAGE, () => json(200, usage()), halfUsed);
    const user = open();
    await ask(user, "How much may I still ask?");
    await screen.findByText(/^You’ve used over 50% of your usage limit/);

    await ask(user, "Count to five.");

    await screen.findByText("One, two, three");
    expect(
      screen.queryByText(/^You’ve used over 50% of your usage limit/),
    ).not.toBeInTheDocument();
  });
});
