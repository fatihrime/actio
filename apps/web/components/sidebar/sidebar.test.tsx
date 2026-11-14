// The list of conversations, driven as a person would: through the whole page,
// against the fake API (tests/fake-api.ts).
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  fakeApi,
  json,
  listed,
  problem,
  streamed,
  summary,
} from "@/tests/fake-api";
import type { Call } from "@/tests/fake-api";
import { ask, open } from "@/tests/open";
import { recorded, RECORDED } from "@/tests/recorded";

const LIST = "GET /api/conversations";
const CONVERSATION = "GET /api/conversations/*";
const RENAME = "PATCH /api/conversations/*";
const DELETE = "DELETE /api/conversations/*";
const TURNS = "POST /api/conversations/*/turns";

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

/** A window too narrow for the list to stand beside the chat. */
function narrowWindow(): void {
  vi.stubGlobal("matchMedia", (query: string) => ({
    media: query,
    matches: false,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  }));
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

  it("stops the answer that was being written in it", async () => {
    const api = fakeApi();
    api
      .on(TURNS, ({ signal }) => {
        // An answer that begins, and then takes its time.
        const body = new ReadableStream<Uint8Array>({
          start(controller) {
            const begun = recorded("answer.sse").split("\n\n").slice(0, 5);
            controller.enqueue(
              new TextEncoder().encode(begun.join("\n\n") + "\n\n"),
            );
            signal?.addEventListener("abort", () => {
              controller.error(new DOMException("Aborted", "AbortError"));
            });
          },
        });
        return new Response(body, {
          headers: {
            "content-type": "text/event-stream; charset=utf-8",
            "x-vercel-ai-ui-message-stream": "v1",
          },
        });
      })
      .on(LIST, () =>
        json(
          200,
          listed(
            api.to(TURNS).map((call) => summary(conversationOf(call), "Sky")),
          ),
        ),
      )
      .on(DELETE, deleted)
      // What the page that was left asks, to learn what was kept of the answer.
      .on(CONVERSATION, () => problem(404, "No such conversation."));
    const user = open();
    await ask(user, "Why is the sky blue?");
    await screen.findByText(/^Echo:/);

    await user.click(
      await screen.findByRole("button", { name: "Delete “Sky”" }),
    );
    await user.click(screen.getByRole("button", { name: "Delete" }));

    await screen.findByText("What would you like to know?");
    expect(api.to(TURNS)[0]?.signal?.aborted).toBe(true);
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
