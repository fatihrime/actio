// The list of conversations, driven as a person would: through the whole page,
// against the fake API (tests/fake-api.ts).
import {
  act,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TITLE_LOOK_MS } from "@/lib/chat/list";
import { LIST_AWAY_MARK } from "@/lib/frame";
import {
  answer,
  conversation,
  fakeApi,
  heldAfter,
  json,
  later,
  listed,
  problem,
  question,
  streamed,
  summary,
  turn,
} from "@/tests/fake-api";
import type { Call } from "@/tests/fake-api";
import { ask, narrowWindow, open } from "@/tests/open";
import { recorded, RECORDED } from "@/tests/recorded";

const LIST = "GET /api/conversations";
const CONVERSATION = "GET /api/conversations/*";
const RENAME = "PATCH /api/conversations/*";
const DELETE = "DELETE /api/conversations/*";
const TURNS = "POST /api/conversations/*/turns";
const STREAM = "GET /api/conversations/*/turns/*/stream";

const SKY = RECORDED.conversation;
const TYRES = "01900000-0000-7000-8000-0000000000c2";
const BREAD = "01900000-0000-7000-8000-0000000000c3";

const tyres = summary(TYRES, "Flat tyres");
const sky = summary(SKY, "Why is the sky blue?");
const bread = summary(BREAD, "Bread without yeast");
/** Three conversations, as the server lists them: the most recently active first. */
const THREE = [tyres, sky, bread];

const deleted = () => new Response(null, { status: 204 });

/** The stored conversation of the API's own recording, whichever one is asked for. */
function storedSky(): Response {
  return new Response(recorded("conversation.json"), {
    headers: { "content-type": "application/json" },
  });
}

/** The conversation id in a call's path. */
function conversationOf(call: Call | undefined): string {
  return call?.path.split("/")[3] ?? "";
}

function theList(): HTMLElement {
  return screen.getByRole("navigation", { name: "Conversations" });
}

/** The titles in the list, from the top. */
function titles(): (string | null)[] {
  return within(theList())
    .queryAllByRole("link")
    .map((link) => link.textContent);
}

/** Let `ms` milliseconds pass, with the page left to do what it does meanwhile. */
async function idle(ms: number): Promise<void> {
  await act(
    () =>
      new Promise<void>((resolve) => {
        setTimeout(resolve, ms);
      }),
  );
}

describe("the list of conversations", () => {
  it("shows them in the order the server gives, and marks the one on the page", async () => {
    fakeApi()
      .on(LIST, () => json(200, listed(THREE)))
      .on(CONVERSATION, storedSky);

    open(`/c/${SKY}`);

    await screen.findByRole("link", { name: "Flat tyres" });
    expect(titles()).toEqual([
      "Flat tyres",
      "Why is the sky blue?",
      "Bread without yeast",
    ]);
    expect(
      screen.getByRole("link", { name: "Why is the sky blue?" }),
    ).toHaveAttribute("aria-current", "page");
    expect(
      screen.getByRole("link", { name: "Flat tyres" }),
    ).not.toHaveAttribute("aria-current");
  });

  it("says so while there are none", async () => {
    fakeApi();

    open();

    expect(await screen.findByText(/No conversations yet/)).toBeInTheDocument();
    expect(within(theList()).queryByRole("link")).not.toBeInTheDocument();
  });

  it("gives a conversation without a title a name to be opened by", async () => {
    fakeApi().on(LIST, () => json(200, listed([summary(TYRES, null)])));

    open();

    expect(
      await screen.findByRole("link", { name: "New conversation" }),
    ).toHaveAttribute("href", `/c/${TYRES}`);
  });

  it("opens a conversation without loading a page", async () => {
    const api = fakeApi()
      .on(LIST, () => json(200, listed(THREE)))
      .on(CONVERSATION, storedSky);
    const user = open();

    await user.click(
      await screen.findByRole("link", { name: "Why is the sky blue?" }),
    );

    expect(
      await screen.findByText("Echo: Why is the sky blue?"),
    ).toBeInTheDocument();
    expect(window.location.pathname).toBe(`/c/${SKY}`);
    expect(conversationOf(api.to(CONVERSATION)[0])).toBe(SKY);
    expect(
      screen.getByRole("link", { name: "Why is the sky blue?" }),
    ).toHaveAttribute("aria-current", "page");
  });

  it("leaves a click with a modifier key to the browser, which opens a new tab", async () => {
    fakeApi().on(LIST, () => json(200, listed(THREE)));
    open();
    const link = await screen.findByRole("link", { name: "Flat tyres" });
    // Whether the page took the click for itself, seen after it has had its say.
    // Taken here in any case: the simulated browser can't open a tab.
    let taken: boolean | undefined;
    window.addEventListener(
      "click",
      (event) => {
        taken = event.defaultPrevented;
        event.preventDefault();
      },
      { once: true },
    );

    fireEvent.click(link, { metaKey: true });

    expect(taken).toBe(false);
    expect(window.location.pathname).toBe("/");
  });

  it("shows a new conversation once the server has it, as the one on the page", async () => {
    const api = fakeApi();
    api
      .on(TURNS, () => streamed("answer.sse"))
      // Empty until the first question was sent; then the server has named the
      // conversation after it.
      .on(LIST, () =>
        json(
          200,
          listed(
            api
              .to(TURNS)
              .map((call) =>
                summary(conversationOf(call), "Why is the sky blue?"),
              ),
          ),
        ),
      );
    const user = open();
    await screen.findByText(/No conversations yet/);

    await ask(user, "Why is the sky blue?");

    const link = await screen.findByRole("link", {
      name: "Why is the sky blue?",
    });
    await waitFor(() => {
      expect(link).toHaveAttribute("aria-current", "page");
    });
    expect(link).toHaveAttribute("href", window.location.pathname);
    expect(screen.queryByText(/No conversations yet/)).not.toBeInTheDocument();
  });

  it("is read again when an answer has ended: the server decides the order", async () => {
    const api = fakeApi();
    api
      .on(CONVERSATION, storedSky)
      .on(TURNS, () => streamed("cut_off.sse"))
      // The conversation that was continued has moved to the top.
      .on(LIST, () =>
        json(
          200,
          listed(api.to(TURNS).length === 0 ? THREE : [sky, tyres, bread]),
        ),
      );
    const user = open(`/c/${SKY}`);
    await screen.findByText("Echo: Why is the sky blue?");
    expect(titles()[0]).toBe("Flat tyres");

    await ask(user, "Count to five.");
    await screen.findByText("One, two, three");

    await waitFor(() => {
      expect(titles()).toEqual([
        "Why is the sky blue?",
        "Flat tyres",
        "Bread without yeast",
      ]);
    });
  });

  it("explains when it can't be read, and reads it when asked again", async () => {
    fakeApi().on(
      LIST,
      () => {
        throw new TypeError("fetch failed");
      },
      () => json(200, listed(THREE)),
    );
    const user = open();

    const notice = await screen.findByText("The list couldn’t be read.");
    expect(
      within(theList()).getByText(/Actio's server can't be reached/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/No conversations yet/)).not.toBeInTheDocument();
    // The chat doesn't depend on the list.
    expect(screen.getByLabelText("Message")).toBeInTheDocument();

    await user.click(
      within(theList()).getByRole("button", { name: "Try again" }),
    );

    expect(
      await screen.findByRole("link", { name: "Flat tyres" }),
    ).toBeInTheDocument();
    expect(notice).not.toBeInTheDocument();
  });

  it("isn't there, nor its button, while there is no session", async () => {
    const api = fakeApi().on("POST /api/session/guest", () =>
      problem(429, "Too many new guests from your network.", {
        retry_after: 120,
      }),
    );

    open();

    await screen.findByText("Too many new guests from your network.");
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Conversations" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "New chat" }),
    ).not.toBeInTheDocument();
    expect(api.to(LIST)).toHaveLength(0);
  });
});

describe("a long list", () => {
  const firstPage = () => json(200, listed([tyres, sky], "after-sky"));
  const secondPage = () => json(200, listed([bread]));

  it("is read a page at a time, each from where the one before ended", async () => {
    const api = fakeApi().on(LIST, (call) =>
      call.query.cursor === undefined ? firstPage() : secondPage(),
    );
    const user = open();
    await screen.findByRole("link", { name: "Flat tyres" });
    expect(titles()).toHaveLength(2);

    await user.click(
      screen.getByRole("button", { name: "Show older conversations" }),
    );

    expect(
      await screen.findByRole("link", { name: "Bread without yeast" }),
    ).toBeInTheDocument();
    expect(titles()).toHaveLength(3);
    expect(api.to(LIST).at(-1)?.query).toEqual({ cursor: "after-sky" });
    // That was the last page.
    expect(
      screen.queryByRole("button", { name: "Show older conversations" }),
    ).not.toBeInTheDocument();
  });

  it("asks again for a page that didn't arrive", async () => {
    let lost = false;
    fakeApi().on(LIST, (call) => {
      if (call.query.cursor === undefined) {
        return firstPage();
      }
      if (!lost) {
        lost = true;
        throw new TypeError("fetch failed");
      }
      return secondPage();
    });
    const user = open();
    const more = await screen.findByRole("button", {
      name: "Show older conversations",
    });

    await user.click(more);
    await screen.findByText("The list couldn’t be read.");
    expect(titles()).toHaveLength(2); // what was read stays
    await user.click(
      screen.getByRole("button", { name: "Show older conversations" }),
    );

    expect(
      await screen.findByRole("link", { name: "Bread without yeast" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("The list couldn’t be read."),
    ).not.toBeInTheDocument();
  });

  it("shows a conversation once, even when two pages both have it", async () => {
    // Pages are read at different moments, and conversations move meanwhile.
    fakeApi().on(LIST, (call) =>
      call.query.cursor === undefined
        ? firstPage()
        : json(200, listed([tyres, bread])),
    );
    const user = open();

    await user.click(
      await screen.findByRole("button", { name: "Show older conversations" }),
    );

    await screen.findByRole("link", { name: "Bread without yeast" });
    expect(titles()).toEqual([
      "Flat tyres",
      "Why is the sky blue?",
      "Bread without yeast",
    ]);
  });
});

// These tests wait in real time, and don't set the clock (`clockAt`): the page's looks
// run on SWR's own timers, which setting the clock doesn't move, and what is measured
// here is how far apart the looks are, by `performance.now()`, which it doesn't set
// either.
describe("a title that is on its way", () => {
  const FIRST_LINE = "How do I fix a flat tyre?";
  /** How long a model takes to write a title here, from when the server first said
   * that one is on its way: about what a model takes. */
  const WRITTEN_MS = 500;
  const SOON = { timeout: 3 * TITLE_LOOK_MS };

  /** Two conversations, each named after its first line, with a title on its way. */
  const beforeTitles = () =>
    json(
      200,
      listed([
        summary(TYRES, FIRST_LINE, { title_pending: true }),
        summary(BREAD, "Can I bake bread without yeast?", {
          title_pending: true,
        }),
      ]),
    );
  /** The same two, named by a model. */
  const withTitles = () =>
    json(200, listed([summary(TYRES, "Flat tyres"), bread]));

  it("shows by itself, the page looking about every second while it is on its way", async () => {
    const api = fakeApi();
    // When each request for the list came, and whether the title is written by then.
    const came: number[] = [];
    let written = false;
    api
      .on(TURNS, () => streamed("answer.sse"))
      .on(LIST, () => {
        came.push(performance.now());
        const [asked] = api.to(TURNS).map((call) => conversationOf(call));
        if (asked === undefined) {
          return json(200, listed([]));
        }
        return written
          ? json(200, listed([summary(asked, "Blue light, scattered")]))
          : json(200, listed([summary(asked, "Sky", { title_pending: true })]));
      });
    const user = open();
    await screen.findByText(/No conversations yet/);

    await ask(user, "Why is the sky blue?");
    await screen.findByRole("link", { name: "Sky" });
    // The answer has ended, and the turn has read the list for the last time: from
    // here on nothing is done, and every request for the list is a look of the page's.
    await screen.findByRole("button", { name: "Send" });
    const turns = came.length;
    await waitFor(
      () => {
        expect(came.length).toBeGreaterThanOrEqual(turns + 3);
      },
      { timeout: 6 * TITLE_LOOK_MS },
    );
    written = true;
    await screen.findByRole("link", { name: "Blue light, scattered" }, SOON);

    // About a second from one look to the next, and not two: SWR by itself would
    // leave out every other look. Between two looks, not from the turn's own
    // readings: when those come is the answer's business.
    const apart = [1, 2].map(
      (look) => (came[turns + look] ?? 0) - (came[turns + look - 1] ?? 0),
    );
    expect(TITLE_LOOK_MS).toBe(1000);
    for (const gap of apart) {
      expect(gap).toBeGreaterThanOrEqual(0.9 * TITLE_LOOK_MS);
      expect(gap).toBeLessThan(1.8 * TITLE_LOOK_MS);
    }
  }, 15_000);

  it("is no longer looked for once the server stops saying that it is on its way", async () => {
    // A title the model didn't give: the server stops saying it is on its way, and
    // the conversation keeps its first line.
    const api = fakeApi();
    let first: number | null = null;
    let over = false;
    api
      .on(TURNS, () => streamed("answer.sse"))
      .on(LIST, () => {
        const [asked] = api.to(TURNS).map((call) => conversationOf(call));
        if (asked === undefined) {
          return json(200, listed([]));
        }
        const now = performance.now();
        first ??= now;
        over = now - first >= WRITTEN_MS;
        const pending = { title_pending: !over };
        return json(200, listed([summary(asked, "Sky", pending)]));
      });
    const user = open();
    await screen.findByText(/No conversations yet/);
    await ask(user, "Why is the sky blue?");
    await waitFor(() => {
      expect(over).toBe(true);
    }, SOON);

    // From here on, a request for the list has no answer, and fails the test.
    api.on(LIST);
    await idle(1.5 * TITLE_LOOK_MS);

    expect(api.unexpected).toEqual([]);
    expect(screen.getByRole("link", { name: "Sky" })).toBeInTheDocument();
  });

  it("isn't looked for when none is", async () => {
    const api = fakeApi().on(LIST, () => json(200, listed(THREE)));
    open();
    await screen.findByRole("link", { name: "Flat tyres" });

    // From here on, a request for the list has no answer, and fails the test.
    api.on(LIST);
    await idle(1.5 * TITLE_LOOK_MS);

    expect(api.unexpected).toEqual([]);
  });

  it("isn't looked for further down the list than a look reads", async () => {
    // A look reads the first page alone. A title on its way in a later page would
    // never be read again by one, so looking for it would never end.
    const api = fakeApi().on(LIST, (call) =>
      call.query.cursor === undefined
        ? json(200, listed([tyres, sky], "after-sky"))
        : json(
            200,
            listed([
              summary(BREAD, "Can I bake bread without yeast?", {
                title_pending: true,
              }),
            ]),
          ),
    );
    const user = open();
    await user.click(
      await screen.findByRole("button", { name: "Show older conversations" }),
    );
    await screen.findByRole("link", {
      name: "Can I bake bread without yeast?",
    });

    // From here on, a request for the list has no answer, and fails the test.
    api.on(LIST);
    await idle(1.5 * TITLE_LOOK_MS);

    expect(api.unexpected).toEqual([]);
  });

  it("is looked for in the first page alone, however many were read", async () => {
    let written = false;
    const api = fakeApi().on(LIST, (call) => {
      if (call.query.cursor !== undefined) {
        return json(200, listed([bread]));
      }
      const tyresNow = written
        ? summary(TYRES, "Flat tyres")
        : summary(TYRES, FIRST_LINE, { title_pending: true });
      return json(200, listed([tyresNow, sky], "after-sky"));
    });
    const user = open();
    await user.click(
      await screen.findByRole("button", { name: "Show older conversations" }),
    );
    await screen.findByRole("link", { name: "Bread without yeast" });
    const before = api.to(LIST).length;
    written = true;

    await screen.findByRole("link", { name: "Flat tyres" }, SOON);

    expect(
      api
        .to(LIST)
        .slice(before)
        .map((call) => call.query),
    ).toEqual([{}]);
    expect(titles()).toEqual([
      "Flat tyres",
      "Why is the sky blue?",
      "Bread without yeast",
    ]);
  });

  it("leaves what was typed in a rename's box, and the keyboard there", async () => {
    const arrival = later(withTitles);
    fakeApi().on(LIST, beforeTitles, arrival.answer);
    const user = open();
    await user.click(
      await screen.findByRole("button", { name: `Rename “${FIRST_LINE}”` }),
    );
    const box = screen.getByRole("textbox", { name: "Title" });
    await user.keyboard("Punctures"); // over the title, which is selected

    arrival.release();
    // The other conversation shows that the titles have arrived.
    await screen.findByRole("link", { name: "Bread without yeast" }, SOON);

    expect(box).toHaveValue("Punctures");
    expect(box).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(
      screen.getByRole("button", { name: "Rename “Flat tyres”" }),
    ).toHaveFocus();
  });

  it("leaves a rename's box as it was opened, for what is typed next", async () => {
    // The box is given its text once, when it opens, and that text is then its own:
    // a title that arrives doesn't change it, though nobody has typed in it yet. The
    // row needs no code for that, and the end-to-end tests hold the same in a real
    // browser (`e2e/sidebar.spec.ts`).
    const arrival = later(withTitles);
    const api = fakeApi()
      .on(LIST, beforeTitles, arrival.answer)
      .on(RENAME, (call) =>
        json(200, summary(conversationOf(call), "Punctures")),
      );
    const user = open();
    await user.click(
      await screen.findByRole("button", { name: `Rename “${FIRST_LINE}”` }),
    );
    const box = screen.getByRole("textbox", { name: "Title" });

    arrival.release();
    await screen.findByRole("link", { name: "Bread without yeast" }, SOON);

    expect(box).toHaveValue(FIRST_LINE);
    expect(box).toHaveFocus();
    await user.keyboard("Punctures{Enter}"); // over the title, still selected
    expect(
      await screen.findByRole("link", { name: "Punctures" }),
    ).toBeInTheDocument();
    expect(api.to(RENAME)[0]?.body).toEqual({ title: "Punctures" });
  });

  it("takes Enter on a rename's box as it was opened for a rename, once another title arrived", async () => {
    // The box still shows the first line, and the conversation is named by a model by
    // now. What the person confirms is what the box shows: their title, not a title
    // they never saw there.
    const arrival = later(withTitles);
    const api = fakeApi()
      .on(LIST, beforeTitles, arrival.answer)
      .on(RENAME, (call) =>
        json(200, summary(conversationOf(call), FIRST_LINE)),
      );
    const user = open();
    await user.click(
      await screen.findByRole("button", { name: `Rename “${FIRST_LINE}”` }),
    );
    const box = screen.getByRole("textbox", { name: "Title" });

    arrival.release();
    await screen.findByRole("link", { name: "Bread without yeast" }, SOON);
    expect(box).toHaveValue(FIRST_LINE);
    await user.keyboard("{Enter}");

    expect(
      await screen.findByRole("link", { name: FIRST_LINE }),
    ).toBeInTheDocument();
    expect(api.to(RENAME).map((call) => call.body)).toEqual([
      { title: FIRST_LINE },
    ]);
  });
});

describe("renaming a conversation", () => {
  it("happens in place, and is shown once the server has done it", async () => {
    const api = fakeApi()
      .on(LIST, () => json(200, listed(THREE)))
      .on(RENAME, (call) =>
        json(200, summary(conversationOf(call), "Punctures")),
      );
    const user = open();

    await user.click(
      await screen.findByRole("button", { name: "Rename “Flat tyres”" }),
    );

    const box = screen.getByRole("textbox", { name: "Title" });
    expect(box).toHaveValue("Flat tyres");
    expect(box).toHaveFocus();
    expect(
      screen.queryByRole("link", { name: "Flat tyres" }),
    ).not.toBeInTheDocument();

    await user.clear(box);
    await user.type(box, "  Punctures {Enter}");

    expect(
      await screen.findByRole("link", { name: "Punctures" }),
    ).toBeInTheDocument();
    const [call] = api.to(RENAME);
    expect(conversationOf(call)).toBe(TYRES);
    expect(call?.body).toEqual({ title: "Punctures" });
    // It keeps its place: renaming is not activity.
    expect(titles()).toEqual([
      "Punctures",
      "Why is the sky blue?",
      "Bread without yeast",
    ]);
    expect(
      screen.getByRole("button", { name: "Rename “Punctures”" }),
    ).toHaveFocus();
  });

  it.each([
    ["Escape", "Something else{Escape}"],
    ["an unchanged title", "{Enter}"],
    ["an empty title", "{Control>}a{/Control}{Backspace}{Enter}"],
  ])("changes nothing with %s", async (_, keys) => {
    const api = fakeApi().on(LIST, () => json(200, listed(THREE)));
    const user = open();
    await user.click(
      await screen.findByRole("button", { name: "Rename “Flat tyres”" }),
    );

    await user.keyboard(keys);

    expect(
      screen.getByRole("link", { name: "Flat tyres" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Title" })).toBeNull();
    expect(api.to(RENAME)).toHaveLength(0);
    expect(
      screen.getByRole("button", { name: "Rename “Flat tyres”" }),
    ).toHaveFocus();
  });

  it("is given up when the box is left", async () => {
    const api = fakeApi().on(LIST, () => json(200, listed(THREE)));
    const user = open();
    await user.click(
      await screen.findByRole("button", { name: "Rename “Flat tyres”" }),
    );
    await user.keyboard("Something else");

    await user.click(screen.getByLabelText("Message"));

    expect(
      screen.getByRole("link", { name: "Flat tyres" }),
    ).toBeInTheDocument();
    expect(api.to(RENAME)).toHaveLength(0);
    expect(screen.getByLabelText("Message")).toHaveFocus();
  });

  it("keeps what was typed when it couldn't be saved, to try again", async () => {
    const api = fakeApi()
      .on(LIST, () => json(200, listed(THREE)))
      .on(
        RENAME,
        () => {
          throw new TypeError("fetch failed");
        },
        (call) => json(200, summary(conversationOf(call), "Punctures")),
      );
    const user = open();
    await user.click(
      await screen.findByRole("button", { name: "Rename “Flat tyres”" }),
    );
    const box = screen.getByRole("textbox", { name: "Title" });
    await user.clear(box);
    await user.type(box, "Punctures{Enter}");

    expect(
      await within(theList()).findByText(/Actio's server can't be reached/),
    ).toBeInTheDocument();
    expect(box).toHaveValue("Punctures");
    expect(box).toHaveFocus();

    await user.keyboard("{Enter}");

    expect(
      await screen.findByRole("link", { name: "Punctures" }),
    ).toBeInTheDocument();
    expect(api.to(RENAME)).toHaveLength(2);
    expect(
      within(theList()).queryByText(/can't be reached/),
    ).not.toBeInTheDocument();
  });

  it("says so when the conversation is gone, and reads the list again", async () => {
    fakeApi()
      .on(
        LIST,
        () => json(200, listed(THREE)),
        // Deleted in another window meanwhile.
        () => json(200, listed([sky, bread])),
      )
      .on(RENAME, () => problem(404, "No such conversation."));
    const user = open();
    await user.click(
      await screen.findByRole("button", { name: "Rename “Flat tyres”" }),
    );
    await user.keyboard("Punctures{Enter}");

    expect(
      await screen.findByText("No such conversation."),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(titles()).toEqual(["Why is the sky blue?", "Bread without yeast"]);
    });
  });
});

describe("deleting a conversation", () => {
  it("asks first, and deletes nothing when the answer is no", async () => {
    const api = fakeApi().on(LIST, () => json(200, listed(THREE)));
    const user = open();

    await user.click(
      await screen.findByRole("button", { name: "Delete “Flat tyres”" }),
    );

    const asking = screen.getByRole("group", { name: "Delete “Flat tyres”?" });
    expect(asking).toHaveTextContent("It can’t be brought back.");
    // The keyboard is on the harmless answer.
    expect(
      within(asking).getByRole("button", { name: "Cancel" }),
    ).toHaveFocus();

    await user.keyboard("{Enter}");

    expect(
      screen.getByRole("link", { name: "Flat tyres" }),
    ).toBeInTheDocument();
    expect(api.to(DELETE)).toHaveLength(0);
    expect(
      screen.getByRole("button", { name: "Delete “Flat tyres”" }),
    ).toHaveFocus();
  });

  it("takes the answer no from Escape too", async () => {
    const api = fakeApi().on(LIST, () => json(200, listed(THREE)));
    const user = open();
    await user.click(
      await screen.findByRole("button", { name: "Delete “Flat tyres”" }),
    );

    await user.keyboard("{Escape}");

    expect(screen.queryByRole("group")).not.toBeInTheDocument();
    expect(api.to(DELETE)).toHaveLength(0);
  });

  it("deletes it on the server, then takes it off the list", async () => {
    const api = fakeApi()
      .on(LIST, () => json(200, listed(THREE)))
      .on(CONVERSATION, storedSky)
      .on(DELETE, deleted);
    const user = open(`/c/${SKY}`);
    await screen.findByText("Echo: Why is the sky blue?");

    await user.click(
      screen.getByRole("button", { name: "Delete “Flat tyres”" }),
    );
    await user.click(screen.getByRole("button", { name: "Delete" }));

    await waitFor(() => {
      expect(titles()).toEqual(["Why is the sky blue?", "Bread without yeast"]);
    });
    expect(conversationOf(api.to(DELETE)[0])).toBe(TYRES);
    // The keyboard is on the row that took its place.
    expect(
      screen.getByRole("link", { name: "Why is the sky blue?" }),
    ).toHaveFocus();
    // Another conversation than the one on the page: that one stays.
    expect(window.location.pathname).toBe(`/c/${SKY}`);
    expect(screen.getByText("Echo: Why is the sky blue?")).toBeInTheDocument();
  });

  it("starts a new conversation when the one on the page was deleted", async () => {
    fakeApi()
      .on(LIST, () => json(200, listed(THREE)))
      .on(CONVERSATION, storedSky)
      .on(DELETE, deleted);
    const user = open(`/c/${SKY}`);
    await screen.findByText("Echo: Why is the sky blue?");

    await user.click(
      screen.getByRole("button", { name: "Delete “Why is the sky blue?”" }),
    );
    await user.click(screen.getByRole("button", { name: "Delete" }));

    expect(
      await screen.findByText("What would you like to know?"),
    ).toBeInTheDocument();
    expect(window.location.pathname).toBe("/");
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
    expect(titles()).toEqual(["Flat tyres", "Bread without yeast"]);
  });

  it("leaves it to the server to stop the answer that was being written in it", async () => {
    const api = fakeApi();
    api
      // An answer that begins, and then takes its time.
      .on(TURNS, heldAfter("answer.sse", 5))
      .on(LIST, () =>
        json(
          200,
          listed(
            api.to(TURNS).map((call) => summary(conversationOf(call), "Sky")),
          ),
        ),
      )
      // The API stops the answer of a conversation it deletes. So the page asks for
      // nothing more: a request to stop the turn, or to read the conversation, would
      // have no answer here, and fail the test.
      .on(DELETE, deleted);
    const user = open();
    await ask(user, "Why is the sky blue?");
    await screen.findByText(/^Echo:/);

    await user.click(
      await screen.findByRole("button", { name: "Delete “Sky”" }),
    );
    await user.click(screen.getByRole("button", { name: "Delete" }));

    await screen.findByText("What would you like to know?");
    expect(api.to(TURNS)[0]?.signal?.aborted).toBe(true); // the page stopped reading
    expect(window.location.pathname).toBe("/");
    expect(screen.getByLabelText("Message")).toHaveValue("");
  });

  it("counts one that is already gone as deleted", async () => {
    fakeApi()
      .on(LIST, () => json(200, listed(THREE)))
      .on(DELETE, () => problem(404, "No such conversation."));
    const user = open();

    await user.click(
      await screen.findByRole("button", {
        name: "Delete “Bread without yeast”",
      }),
    );
    await user.click(screen.getByRole("button", { name: "Delete" }));

    await waitFor(() => {
      expect(titles()).toEqual(["Flat tyres", "Why is the sky blue?"]);
    });
    expect(screen.queryByText("No such conversation.")).not.toBeInTheDocument();
    // It was the last row: the keyboard is on the one before it.
    expect(
      screen.getByRole("link", { name: "Why is the sky blue?" }),
    ).toHaveFocus();
  });

  it("keeps it, and says why, when the server couldn't delete it", async () => {
    fakeApi()
      .on(LIST, () => json(200, listed(THREE)))
      .on(DELETE, () => {
        throw new TypeError("fetch failed");
      });
    const user = open();

    await user.click(
      await screen.findByRole("button", { name: "Delete “Flat tyres”" }),
    );
    await user.click(screen.getByRole("button", { name: "Delete" }));

    expect(
      await within(theList()).findByText(/Actio's server can't be reached/),
    ).toBeInTheDocument();
    expect(titles()).toHaveLength(3);
    expect(
      screen.getByRole("button", { name: "Delete “Flat tyres”" }),
    ).toHaveFocus();

    await user.click(screen.getByRole("button", { name: "Dismiss" }));

    expect(
      within(theList()).queryByText(/can't be reached/),
    ).not.toBeInTheDocument();
  });
});

describe("opening another conversation while an answer is being written", () => {
  it("lets the answer be written on, and reads along with it on coming back", async () => {
    const api = fakeApi();
    const asked = () => conversationOf(api.to(TURNS)[0]);
    let looks = 0;
    api
      // An answer that begins, and then takes its time: longer than its reader stays.
      .on(TURNS, heldAfter("answer.sse", 5))
      .on(LIST, () =>
        json(
          200,
          listed([
            ...api
              .to(TURNS)
              .map((call) => summary(conversationOf(call), "Sky")),
            tyres,
          ]),
        ),
      )
      .on(CONVERSATION, (call) => {
        if (conversationOf(call) === TYRES) {
          return json(
            200,
            conversation(TYRES, [
              question("q-tyres", "How do I fix a flat tyre?"),
              answer("a-tyres", "With a patch.", "q-tyres"),
            ]),
          );
        }
        // The one that was left: still being written when it is opened again.
        looks += 1;
        const sent = (api.to(TURNS)[0]?.body as { message: { id: string } })
          .message.id;
        return json(
          200,
          conversation(
            asked(),
            [question(sent, "Why is the sky blue?")],
            turn({ status: "running", finish_reason: null }),
          ),
        );
      })
      // The page reads along with it again, from the answer's first event.
      .on(STREAM, () => streamed("answer.sse"));
    const user = open();
    await ask(user, "Why is the sky blue?");
    await screen.findByText(/^Echo:/);

    await user.click(await screen.findByRole("link", { name: "Flat tyres" }));
    expect(await screen.findByText("With a patch.")).toBeInTheDocument();
    // The page stopped reading the answer. It asked nobody to stop writing it: a
    // request for that would have no answer here, and fail the test.
    expect(api.to(TURNS)[0]?.signal?.aborted).toBe(true);
    const usageRead = api.to("GET /api/usage").length;

    await user.click(screen.getByRole("link", { name: "Sky" }));

    expect(
      await screen.findByText("Echo: Why is the sky blue?"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/still being written/)).not.toBeInTheDocument();
    expect(api.to(STREAM).map((call) => call.path)).toEqual([
      `/api/conversations/${asked()}/turns/${RECORDED.turn}/stream`,
    ]);
    expect(looks).toBe(1);
    // What it cost is read once it has ended.
    await waitFor(() => {
      expect(api.to("GET /api/usage").length).toBeGreaterThan(usageRead);
    });
  });
});

describe("where the list is", () => {
  const listButton = () =>
    screen.findByRole("button", { name: "Conversations" });

  it("stands beside the chat in a wide window, until it is put away", async () => {
    fakeApi().on(LIST, () => json(200, listed(THREE)));
    const user = open();
    await screen.findByRole("link", { name: "Flat tyres" });
    expect(await listButton()).toHaveAttribute("aria-expanded", "true");

    await user.click(await listButton());

    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
    expect(await listButton()).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByLabelText("Message")).toBeInTheDocument();

    await user.click(await listButton());

    expect(
      screen.getByRole("link", { name: "Flat tyres" }),
    ).toBeInTheDocument();
  });

  it("stays away on the next visit, once put away", async () => {
    fakeApi().on(LIST, () => json(200, listed(THREE)));
    const user = open();
    await user.click(await listButton());
    document.body.replaceChildren(); // the page is closed

    open();

    expect(await listButton()).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
  });

  it.each([
    ["", false],
    [", in StrictMode as in development", true],
  ])(
    "is said to be away on the page's root while it is, from the start of the next visit%s",
    async (_, strict) => {
      // What decides the list's place before the session is known (lib/frame.ts).
      fakeApi().on(LIST, () => json(200, listed(THREE)));
      const root = document.documentElement;
      const user = open("/", { strict });
      await screen.findByRole("link", { name: "Flat tyres" });
      expect(root).not.toHaveAttribute(LIST_AWAY_MARK);

      await user.click(await listButton());
      expect(root).toHaveAttribute(LIST_AWAY_MARK);
      await user.click(await listButton());
      expect(root).not.toHaveAttribute(LIST_AWAY_MARK);

      await user.click(await listButton());
      document.body.replaceChildren(); // the page is closed
      // A new page starts without the mark, until the script at the start of its body
      // sets it (lib/frame.test.ts). The page sets it too, as it starts.
      root.removeAttribute(LIST_AWAY_MARK);
      open("/", { strict });

      expect(screen.getByText("Starting…")).toBeInTheDocument();
      expect(root).toHaveAttribute(LIST_AWAY_MARK);
      expect(await listButton()).toHaveAttribute("aria-expanded", "false");
      expect(root).toHaveAttribute(LIST_AWAY_MARK);
    },
  );

  it("lies over the chat in a narrow window, only while it is wanted", async () => {
    narrowWindow();
    fakeApi()
      .on(LIST, () => json(200, listed(THREE)))
      .on(CONVERSATION, storedSky);
    const user = open();
    expect(await listButton()).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();

    await user.click(await listButton());
    await user.click(
      await screen.findByRole("link", { name: "Why is the sky blue?" }),
    );

    // Choosing one puts the list away again: the chat is what was asked for.
    expect(
      await screen.findByText("Echo: Why is the sky blue?"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
    expect(window.localStorage.length).toBe(0); // nothing to remember here
  });

  it("goes away in a narrow window with Escape, a click beside it, or New chat", async () => {
    narrowWindow();
    fakeApi().on(LIST, () => json(200, listed(THREE)));
    const user = open();

    await user.click(await listButton());
    await screen.findByRole("link", { name: "Flat tyres" });
    // The keyboard went along: the next Tab is the first conversation.
    expect(theList()).toHaveFocus();
    await user.keyboard("{Tab}");
    expect(screen.getByRole("link", { name: "Flat tyres" })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
    expect(await listButton()).toHaveFocus();

    // From anywhere on the page, not only from inside the list.
    await user.click(await listButton());
    await user.keyboard("{Shift>}{Tab}{/Shift}{Escape}");
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();

    await user.click(await listButton());
    await user.click(
      screen.getByRole("button", { name: "Close the conversations" }),
    );
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();

    await user.click(await listButton());
    await user.click(screen.getByRole("button", { name: "New chat" }));
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
  });

  it("keeps Escape for the title that is being typed", async () => {
    narrowWindow();
    fakeApi().on(LIST, () => json(200, listed(THREE)));
    const user = open();
    await user.click(await listButton());
    await user.click(
      await screen.findByRole("button", { name: "Rename “Flat tyres”" }),
    );

    await user.keyboard("{Escape}");

    // The renaming ended; the list is still there.
    expect(
      screen.getByRole("link", { name: "Flat tyres" }),
    ).toBeInTheDocument();
  });
});

describe("a conversation that was started here, and deleted", () => {
  it("is gone at its address too: nothing of it stays on the page", async () => {
    const api = fakeApi();
    api
      .on(TURNS, () => streamed("answer.sse"))
      .on(LIST, () =>
        json(
          200,
          listed(
            api.to(DELETE).length > 0
              ? []
              : api
                  .to(TURNS)
                  .map((call) => summary(conversationOf(call), "Sky")),
          ),
        ),
      )
      .on(DELETE, deleted)
      .on(CONVERSATION, () => problem(404, "No such conversation."));
    const user = open();
    await ask(user, "Why is the sky blue?");
    await screen.findByText("Echo: Why is the sky blue?");
    const address = window.location.pathname;

    await user.click(
      await screen.findByRole("button", { name: "Delete “Sky”" }),
    );
    await user.click(screen.getByRole("button", { name: "Delete" }));
    await screen.findByText("What would you like to know?");
    // Someone returns to it by its address.
    window.history.pushState(null, "", address);
    window.dispatchEvent(new PopStateEvent("popstate"));

    expect(
      await screen.findByText(/doesn’t exist, or belongs to another session/),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Message")).not.toBeInTheDocument();
  });
});
