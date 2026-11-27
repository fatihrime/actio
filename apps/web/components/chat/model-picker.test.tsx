// The picker of the model that answers (components/chat/model-picker.tsx, ADR 0032),
// driven as a person would: through the whole page, against the fake API
// (tests/fake-api.ts). The rule for which model answers and what a question names is
// lib/chat/chosen.ts's, and tested there too; here, what the page does with it. When a
// chosen model is asked to be loaded ahead is in warm-up.test.tsx, which loads the
// app's modules anew for each test.
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import type { UserEvent } from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { clockAt, SECOND } from "@/tests/clock";
import {
  answer,
  conversation,
  fakeApi,
  freeModel,
  json,
  later,
  model,
  problem,
  question,
  refused,
  secondModel,
  sentByHand,
  streamed,
} from "@/tests/fake-api";
import type { Call, FakeApi } from "@/tests/fake-api";
import { ask, narrowWindow, open } from "@/tests/open";

const MODELS = "GET /api/models";
const TURNS = "POST /api/conversations/*/turns";
const CANCEL = "POST /api/conversations/*/turns/*/cancel";
const CONVERSATION = "GET /api/conversations/*";
const CHOSEN = "actio.model";
const SECOND_ANSWER = "01900000-0000-7000-8000-000000000004";
const STORED = "01900000-0000-7000-8000-0000000000c9";

const DEFAULT = "Qwen 3.5 4B";
const REACHED = "You’ve reached your usage limit for this session.";

/** How long the page takes, at the most, to read the models again while the one that
 * answers can't: it looks every five seconds. */
const NEXT_LOOK = { timeout: 8 * SECOND };
/** How long the page may take to notice that the clock was moved: it looks at least
 * once a second. */
const SOON = { timeout: 3000 };

/** An installation that offers two models: the default, which can reason, and a second,
 * which can't and costs nothing. `changes` says how the second differs. */
const twoModels = (changes: Parameters<typeof freeModel>[0] = {}) =>
  json(200, { default: "standard", models: [model(), freeModel(changes)] });

/** What the API says of a conversation whose first question it refused. */
const missing = () => problem(404, "No such conversation.");

/** The picker's button, once it is there. */
function picker(): Promise<HTMLElement> {
  return screen.findByRole("button", { name: /^Model: / });
}

/** The menu, shown or not: the page's one. Hidden, it has no name to be found by. */
function menu(): HTMLElement {
  return screen.getByRole("menu", { hidden: true });
}

/** A row of the menu, while it is shown. */
function row(name: string): HTMLElement {
  return within(menu()).getByRole("menuitemradio", { name });
}

function box(): HTMLElement {
  return screen.getByLabelText("Message");
}

function send(): HTMLElement {
  return screen.getByRole("button", { name: "Send" });
}

/** Open the menu with the mouse, and choose `name` in it. */
async function choose(user: UserEvent, name: string): Promise<void> {
  await user.click(await picker());
  await user.click(row(name));
}

/** The model each question named, in the order they were sent: nothing for one that
 * named none. */
function named(api: FakeApi): (string | undefined)[] {
  return api
    .to(TURNS)
    .map((call: Call) => (call.body as { model?: string }).model);
}

describe("the model picker", () => {
  it("isn't on the page with one model on offer, in what it draws or in the DOM", async () => {
    fakeApi();

    open();

    // The models are read: the line under the empty page names the one that answers.
    await screen.findByText(/Answered by Qwen 3\.5 4B,/);
    expect(
      screen.queryByRole("button", { name: /^Model: /, hidden: true }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("menu", { hidden: true }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryAllByRole("menuitemradio", { hidden: true }),
    ).toHaveLength(0);
    // The switch for reasoning says all of its words, as it did before there was a
    // picker, and is named by them alone.
    const reasoning = screen.getByLabelText("Think before answering");
    expect(reasoning).not.toHaveAttribute("aria-label");
    expect(reasoning.closest("label")).toHaveTextContent(
      /^Think before answering$/,
    );
    // It stands in the box's row itself, with Send and nothing else: no place is made
    // around it for a picker that isn't there.
    const inRow = reasoning.closest("label")?.parentElement;
    expect(inRow?.parentElement?.tagName).toBe("FORM");
    expect(inRow?.children).toHaveLength(2);
  });

  it("isn't there until the models are read", async () => {
    const models = later(() => twoModels());
    fakeApi().on(MODELS, models.answer);

    open();

    await screen.findByLabelText("Message");
    expect(
      screen.queryByRole("button", { name: /^Model: /, hidden: true }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("menu", { hidden: true }),
    ).not.toBeInTheDocument();

    models.release();

    expect(await picker()).toHaveAccessibleName(`Model: ${DEFAULT}`);
  });

  it("names the model that answers, and opens a menu of the models above it, that one checked", async () => {
    fakeApi().on(MODELS, () => twoModels());
    const user = open();

    const button = await picker();

    expect(button).toHaveAccessibleName(`Model: ${DEFAULT}`);
    expect(button).toHaveTextContent(DEFAULT);
    expect(button).toHaveAttribute("aria-haspopup", "menu");
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(button).toHaveAttribute("aria-controls", menu().id);
    // On the page from when the button is, and hidden while it is closed.
    expect(menu()).not.toBeVisible();
    // Beside it, the switch for reasoning, still named by all of its words: by a name
    // of its own, since in little room it shows "Think" alone. This page has no
    // styles, and finds every word in the label either way; a real browser, in a
    // window of 320 px, is asked in e2e/picker.spec.ts.
    expect(screen.getByLabelText("Think before answering")).toHaveAttribute(
      "aria-label",
      "Think before answering",
    );

    await user.click(button);

    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(menu()).toBeVisible();
    expect(menu()).toHaveAccessibleName("Model");
    expect(
      within(menu())
        .getAllByRole("menuitemradio")
        .map((each) => each.textContent),
    ).toEqual([DEFAULT, "Local"]);
    expect(row(DEFAULT)).toHaveAttribute("aria-checked", "true");
    expect(row("Local")).toHaveAttribute("aria-checked", "false");
    // Opening puts the keyboard on the checked row.
    expect(row(DEFAULT)).toHaveFocus();
  });

  it("chooses by mouse, and the model chosen answers, named in the question", async () => {
    const api = fakeApi()
      .on(MODELS, () => twoModels())
      .on(TURNS, () => streamed("answer.sse"));
    const user = open();

    await choose(user, "Local");

    const button = await picker();
    expect(menu()).not.toBeVisible();
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(button).toHaveAccessibleName("Model: Local");
    // The keyboard is back on the button.
    expect(button).toHaveFocus();
    expect(window.localStorage.getItem(CHOSEN)).toBe("local");
    expect(screen.getByText(/Answered by Local,/)).toBeInTheDocument();

    await ask(user, "Why is the sky blue?");

    await screen.findByText("Echo: Why is the sky blue?");
    expect(named(api)).toEqual(["local"]);
  });

  it("chooses by keys, and puts the keyboard back on the button", async () => {
    fakeApi().on(MODELS, () => twoModels());
    const user = open();
    const button = await picker();
    await user.click(box());

    await user.tab();
    expect(button).toHaveFocus();

    // Enter opens, on the checked row; the arrows go round the ends, Home and End to
    // them; Enter chooses.
    await user.keyboard("{Enter}");
    expect(menu()).toBeVisible();
    expect(row(DEFAULT)).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(row("Local")).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(row(DEFAULT)).toHaveFocus();
    await user.keyboard("{ArrowUp}");
    expect(row("Local")).toHaveFocus();
    await user.keyboard("{Home}");
    expect(row(DEFAULT)).toHaveFocus();
    await user.keyboard("{End}");
    expect(row("Local")).toHaveFocus();
    await user.keyboard("{Enter}");

    expect(menu()).not.toBeVisible();
    expect(button).toHaveFocus();
    expect(button).toHaveAccessibleName("Model: Local");

    // Space opens and chooses too, and ArrowUp opens.
    await user.keyboard(" ");
    expect(row("Local")).toHaveFocus();
    await user.keyboard("{ArrowUp} ");
    expect(button).toHaveFocus();
    expect(button).toHaveAccessibleName(`Model: ${DEFAULT}`);
    await user.keyboard("{ArrowUp}");
    expect(menu()).toBeVisible();
    expect(row(DEFAULT)).toHaveFocus();
    await user.keyboard("{Escape}{ArrowDown}");
    expect(menu()).toBeVisible();
    expect(row(DEFAULT)).toHaveFocus();
  });

  it("moves up and down among three models, which two can't tell apart", async () => {
    fakeApi().on(MODELS, () =>
      json(200, {
        default: "standard",
        models: [model(), freeModel(), secondModel()],
      }),
    );
    const user = open();
    await user.click(await picker());
    expect(row(DEFAULT)).toHaveFocus();

    await user.keyboard("{ArrowDown}");
    expect(row("Local")).toHaveFocus();
    await user.keyboard("{ArrowUp}");
    expect(row(DEFAULT)).toHaveFocus();
    // Round the ends, either way.
    await user.keyboard("{ArrowUp}");
    expect(row("Second model")).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(row(DEFAULT)).toHaveFocus();
  });

  it("keeps the keyboard at a press on the menu's own edge, between its rows", async () => {
    fakeApi().on(MODELS, () => twoModels());
    const user = open();
    const button = await picker();
    await user.click(button);
    await user.keyboard("{ArrowDown}");
    expect(row("Local")).toHaveFocus();

    // The press lands on the menu itself, which takes the keyboard: on nothing that
    // could, it left the picker, and no key of the picker's did anything after.
    await user.pointer({ keys: "[MouseLeft]", target: menu() });

    expect(menu()).toBeVisible();
    expect(menu()).toHaveFocus();
    // The arrows go on from there, to the first row or the last.
    await user.keyboard("{ArrowUp}");
    expect(row("Local")).toHaveFocus();
    await user.pointer({ keys: "[MouseLeft]", target: menu() });
    await user.keyboard("{ArrowDown}");
    expect(row(DEFAULT)).toHaveFocus();
    await user.pointer({ keys: "[MouseLeft]", target: menu() });
    await user.keyboard("{Escape}");

    expect(menu()).not.toBeVisible();
    expect(button).toHaveFocus();
    expect(button).toHaveAccessibleName(`Model: ${DEFAULT}`);
  });

  it("closes with Escape, and chooses nothing", async () => {
    fakeApi().on(MODELS, () => twoModels());
    const user = open();
    const button = await picker();
    await user.click(button);
    await user.keyboard("{ArrowDown}");
    expect(row("Local")).toHaveFocus();

    await user.keyboard("{Escape}");

    expect(menu()).not.toBeVisible();
    expect(button).toHaveFocus();
    expect(button).toHaveAccessibleName(`Model: ${DEFAULT}`);
    expect(window.localStorage.getItem(CHOSEN)).toBeNull();
  });

  it("closes with a click on its button too, the keyboard on it", async () => {
    fakeApi().on(MODELS, () => twoModels());
    const user = open();
    const button = await picker();
    await user.click(button);

    await user.click(button);

    expect(menu()).not.toBeVisible();
    expect(button).toHaveFocus();
  });

  it("closes with Tab, and the keyboard goes where Tab takes it", async () => {
    fakeApi().on(MODELS, () => twoModels());
    const user = open();
    const button = await picker();
    await user.click(button);

    await user.tab();

    // The next thing after the picker: the switch for reasoning. Where a real browser
    // goes on from a row that was hidden while its key was handled, a real browser
    // says: e2e/picker.spec.ts.
    expect(screen.getByLabelText("Think before answering")).toHaveFocus();
    expect(menu()).not.toBeVisible();

    await user.click(button);
    await user.tab({ shift: true });

    expect(button).toHaveFocus();
    expect(menu()).not.toBeVisible();
    expect(button).toHaveAccessibleName(`Model: ${DEFAULT}`);
  });

  it("closes at a press outside it, and puts the keyboard nowhere itself", async () => {
    fakeApi().on(MODELS, () => twoModels());
    const user = open();
    const button = await picker();
    await user.click(button);

    // A press inside the menu, beside its rows, keeps it open.
    await user.pointer({ keys: "[MouseLeft]", target: menu() });
    expect(menu()).toBeVisible();

    await user.click(screen.getByText("What would you like to know?"));

    expect(menu()).not.toBeVisible();
    expect(button).not.toHaveFocus();
    expect(button).toHaveAccessibleName(`Model: ${DEFAULT}`);

    // Pressed on the box, the keyboard is where the press put it.
    await user.click(button);
    await user.click(box());

    expect(menu()).not.toBeVisible();
    expect(box()).toHaveFocus();
  });

  it("keeps Escape to itself, in a narrow window where the list of conversations lies over the chat", async () => {
    narrowWindow();
    fakeApi().on(MODELS, () => twoModels());
    const user = open();
    const button = await picker();
    await user.click(screen.getByRole("button", { name: "Conversations" }));
    await screen.findByRole("navigation", { name: "Conversations" });
    // The keyboard leaves the list for the chat beneath it, and reaches the picker.
    for (let step = 0; step < 40 && document.activeElement !== button; step++) {
      await user.tab();
    }
    expect(button).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(menu()).toBeVisible();

    await user.keyboard("{Escape}");

    expect(menu()).not.toBeVisible();
    expect(button).toHaveFocus();
    expect(
      screen.getByRole("navigation", { name: "Conversations" }),
    ).toBeInTheDocument();

    // With the menu closed, Escape is the list's again.
    await user.keyboard("{Escape}");

    expect(
      screen.queryByRole("navigation", { name: "Conversations" }),
    ).not.toBeInTheDocument();
  });

  it("sends no question when a model is chosen with Enter", async () => {
    const api = fakeApi()
      .on(MODELS, () => twoModels())
      .on(TURNS, () => streamed("answer.sse"));
    const user = open();
    const button = await picker();
    await user.type(box(), "Why is the sky blue?");
    await user.tab();
    expect(button).toHaveFocus();

    await user.keyboard("{Enter}{ArrowDown}{Enter}");

    expect(button).toHaveAccessibleName("Model: Local");
    expect(api.to(TURNS)).toHaveLength(0);
    expect(box()).toHaveValue("Why is the sky blue?");
    // None of its buttons sends the form it stands in.
    for (const each of [
      button,
      ...within(menu()).getAllByRole("menuitemradio", { hidden: true }),
    ]) {
      expect(each).toHaveAttribute("type", "button");
    }
  });

  it("is used while an answer is being written, which goes on as it was", async () => {
    const writing = sentByHand("answer.sse");
    const api = fakeApi()
      .on(MODELS, () => twoModels())
      .on(TURNS, writing.answer, () => streamed("cut_off.sse", SECOND_ANSWER));
    const user = open();
    await ask(user, "Why is the sky blue?");
    writing.send(5);
    await screen.findByRole("button", { name: "Stop" });

    await choose(user, "Local");

    expect(await picker()).toHaveAccessibleName("Model: Local");
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();
    writing.end();
    await screen.findByText("Echo: Why is the sky blue?");
    expect(api.to(CANCEL)).toHaveLength(0);

    await ask(user, "Count to five.");

    await screen.findByText("One, two, three");
    // The answer under way was the default's, as it was asked; the next is Local's.
    expect(named(api)).toEqual([undefined, "local"]);
  });

  it("is the browser's choice: kept over a new visit, in a new conversation and in a stored one", async () => {
    const api = fakeApi()
      .on(MODELS, () => twoModels())
      .on(TURNS, () => streamed("answer.sse"))
      .on(CONVERSATION, () =>
        json(
          200,
          conversation(STORED, [
            question("01900000-0000-7000-8000-0000000000d1", "Hello?"),
            answer(
              "01900000-0000-7000-8000-0000000000d2",
              "Hello.",
              "01900000-0000-7000-8000-0000000000d1",
            ),
          ]),
        ),
      );
    const user = open();
    await choose(user, "Local");

    // The page is opened anew.
    cleanup();
    const again = open();

    expect(await picker()).toHaveAccessibleName("Model: Local");
    await again.click(screen.getByRole("button", { name: "New chat" }));
    expect(await picker()).toHaveAccessibleName("Model: Local");

    cleanup();
    const stored = open(`/c/${STORED}`);

    await screen.findByText("Hello.");
    expect(await picker()).toHaveAccessibleName("Model: Local");
    await ask(stored, "Why is the sky blue?");
    await screen.findByText("Echo: Why is the sky blue?");
    expect(named(api)).toEqual(["local"]);
  });

  it("names the stored choice in a question sent before the models are read", async () => {
    window.localStorage.setItem(CHOSEN, "local");
    const models = later(() => twoModels());
    const api = fakeApi()
      .on(MODELS, models.answer)
      .on(TURNS, () => streamed("answer.sse"));
    const user = open();

    await ask(user, "Why is the sky blue?");

    await screen.findByText("Echo: Why is the sky blue?");
    expect(named(api)).toEqual(["local"]);
    models.release();
    expect(await picker()).toHaveAccessibleName("Model: Local");
  });

  it("leaves a stored choice that isn't on offer, and the default answers, named by none", async () => {
    window.localStorage.setItem(CHOSEN, "gone");
    const api = fakeApi()
      .on(MODELS, () => twoModels())
      .on(TURNS, () => streamed("answer.sse"));
    const user = open();

    const button = await picker();

    expect(button).toHaveAccessibleName(`Model: ${DEFAULT}`);
    await user.click(button);
    expect(row(DEFAULT)).toHaveAttribute("aria-checked", "true");
    await user.keyboard("{Escape}");
    await ask(user, "Why is the sky blue?");
    await screen.findByText("Echo: Why is the sky blue?");
    expect(named(api)).toEqual([undefined]);
    expect(window.localStorage.getItem(CHOSEN)).toBe("gone");
  });

  it("with two models on offer, and never opened, sends the question as it was sent before there was a picker", async () => {
    const api = fakeApi()
      .on(MODELS, () => twoModels())
      .on(TURNS, () => streamed("answer.sse"));
    const user = open();
    await picker();

    await ask(user, "Why is the sky blue?");

    await screen.findByText("Echo: Why is the sky blue?");
    expect(api.to(TURNS)[0]?.body).toEqual({
      message: {
        id: expect.any(String) as unknown,
        parts: [{ type: "text", text: "Why is the sky blue?" }],
      },
      parent_id: null,
      thinking: false,
    });
  });

  it("offers a model that can't answer, says why in its row, and once it is chosen what to do, until it can", async () => {
    const offered = (localStatus: "missing" | "ready") =>
      json(200, {
        default: "standard",
        models: [
          model(),
          freeModel({ status: localStatus }),
          secondModel({ status: "unreachable" }),
        ],
      });
    const api = fakeApi().on(
      MODELS,
      // When the page opens, and when the menu is opened.
      () => offered("missing"),
      () => offered("missing"),
      () => offered("ready"),
    );
    const user = open();
    await user.click(await picker());

    expect(row(DEFAULT)).not.toHaveAccessibleDescription();
    expect(row("Local")).toHaveAccessibleDescription("Isn't downloaded yet");
    expect(row("Second model")).toHaveAccessibleDescription("Can't be reached");

    await user.click(row("Local"));

    expect(
      await screen.findByText("Local isn't downloaded yet."),
    ).toBeInTheDocument();
    expect(screen.getByText("ollama pull granite4:micro")).toBeInTheDocument();
    await user.type(box(), "Hello");
    expect(send()).toBeDisabled();
    expect(send()).toHaveAttribute(
      "title",
      "The model can't answer right now.",
    );
    // The default can answer: it is the chosen model's state the page looks at again.
    await waitFor(() => {
      expect(
        screen.queryByText("Local isn't downloaded yet."),
      ).not.toBeInTheDocument();
    }, NEXT_LOOK);
    expect(api.to(MODELS)).toHaveLength(3);
    expect(send()).toBeEnabled();
  }, 15_000);

  it("reads the models again when it opens, and a row says what changed", async () => {
    const changed = later(() => twoModels({ status: "unreachable" }));
    const api = fakeApi().on(MODELS, () => twoModels(), changed.answer);
    const user = open();
    const button = await picker();
    expect(api.to(MODELS)).toHaveLength(1);

    await user.click(button);

    // What was known is shown at once, and the row changes when the answer comes.
    await waitFor(() => {
      expect(api.to(MODELS)).toHaveLength(2);
    });
    expect(row("Local")).not.toHaveAccessibleDescription();
    changed.release();
    await waitFor(() => {
      expect(row("Local")).toHaveAccessibleDescription("Can't be reached");
    });
    expect(api.to(MODELS)).toHaveLength(2);
    // The keyboard stayed where opening put it.
    expect(row(DEFAULT)).toHaveFocus();

    await user.keyboard("{Escape}{Enter}");

    await waitFor(() => {
      expect(api.to(MODELS)).toHaveLength(3);
    });
  });

  it("brings the switch for reasoning with the model that can, and asks only that one to", async () => {
    window.localStorage.setItem("actio.thinking", "on");
    const api = fakeApi()
      .on(MODELS, () => twoModels())
      .on(
        TURNS,
        () => streamed("answer.sse"),
        () => streamed("cut_off.sse", SECOND_ANSWER),
      );
    const user = open();
    expect(
      await screen.findByLabelText("Think before answering"),
    ).toBeChecked();

    await choose(user, "Local");

    expect(
      screen.queryByLabelText("Think before answering"),
    ).not.toBeInTheDocument();
    await ask(user, "Why is the sky blue?");
    await screen.findByText("Echo: Why is the sky blue?");

    await choose(user, DEFAULT);

    expect(screen.getByLabelText("Think before answering")).toBeChecked();
    await ask(user, "Count to five.");
    await screen.findByText("One, two, three");
    expect(api.to(TURNS).map((call) => call.body)).toMatchObject([
      { model: "local", thinking: false },
      { model: "standard", thinking: true },
    ]);
  });

  it("leaves the line under the box empty for another model than the one the figure is of", async () => {
    fakeApi()
      .on(MODELS, () => twoModels())
      .on(TURNS, () => streamed("answer.sse"));
    const user = open();
    await ask(user, "Why is the sky blue?");
    await screen.findByText("Echo: Why is the sky blue?");
    // The default model answered, and counted its window.
    const meter = await screen.findByRole("meter", { name: "Context" });
    expect(meter).toHaveAttribute(
      "aria-valuetext",
      "under 1% full: 36 of 8,192 tokens",
    );

    await choose(user, "Local");

    expect(
      screen.queryByRole("meter", { name: "Context" }),
    ).not.toBeInTheDocument();

    await choose(user, DEFAULT);

    expect(screen.getByRole("meter", { name: "Context" })).toHaveAttribute(
      "aria-valuetext",
      "under 1% full: 36 of 8,192 tokens",
    );
  });
});

describe("the model picker at a usage limit", () => {
  it("names the model that stands in, and a choice in it ends the standing in", async () => {
    clockAt();
    const api = fakeApi()
      .on(MODELS, () => twoModels())
      .on(
        TURNS,
        () => refused("limit_reached.json"),
        () => streamed("answer.sse"),
      )
      .on(CONVERSATION, missing);
    const user = open();
    await ask(user, "Why is the sky blue?");
    await user.click(
      await screen.findByRole("button", { name: "Use Local (free)" }),
    );
    await screen.findByText("Local is answering, at no cost.");

    // The one that stands in, in the button and in the menu.
    const button = await picker();
    expect(button).toHaveAccessibleName("Model: Local");
    await user.click(button);
    expect(row("Local")).toHaveAttribute("aria-checked", "true");

    // The reader's newest decision counts: the default, which the limit holds.
    await user.click(row(DEFAULT));

    expect(button).toHaveAccessibleName(`Model: ${DEFAULT}`);
    expect(
      screen.queryByText("Local is answering, at no cost."),
    ).not.toBeInTheDocument();
    expect(screen.getByText(REACHED)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Use Local (free)" }),
    ).toBeInTheDocument();
    expect(send()).toBeDisabled();

    // Chosen in the picker, the model that costs nothing answers as it did by the
    // notice's button.
    await choose(user, "Local");

    expect(
      screen.getByText("Local is answering, at no cost."),
    ).toBeInTheDocument();
    expect(send()).toBeEnabled();
    await user.click(send());
    await screen.findByText("Echo: Why is the sky blue?");
    expect(named(api)).toEqual([undefined, "local"]);
  });

  it("gives the reader's earlier choice back when the limit has started over, named in the question", async () => {
    const clock = clockAt();
    window.localStorage.setItem(CHOSEN, "second");
    const api = fakeApi()
      .on(MODELS, () =>
        json(200, {
          default: "standard",
          models: [model(), freeModel(), secondModel()],
        }),
      )
      .on(
        TURNS,
        () => refused("limit_reached.json", { retry_after: 1 }),
        () => streamed("answer.sse"),
      )
      .on(CONVERSATION, missing);
    const user = open();
    expect(await picker()).toHaveAccessibleName("Model: Second model");
    await ask(user, "Why is the sky blue?");
    await user.click(
      await screen.findByRole("button", { name: "Use Local (free)" }),
    );
    expect(await picker()).toHaveAccessibleName("Model: Local");

    clock.advance(2 * SECOND);

    await waitFor(() => {
      expect(
        screen.queryByText("Local is answering, at no cost."),
      ).not.toBeInTheDocument();
    }, SOON);
    expect(await picker()).toHaveAccessibleName("Model: Second model");
    await user.click(send());
    await screen.findByText("Echo: Why is the sky blue?");
    expect(named(api)).toEqual(["second", "second"]);
    expect(window.localStorage.getItem(CHOSEN)).toBe("second");
  });
});
