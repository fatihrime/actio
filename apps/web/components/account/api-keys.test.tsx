// An account's API keys, in its place on the page, driven as a person would: through
// the whole page, against the fake API (tests/fake-api.ts). No whole key is written
// here: each is drawn as the test runs (`madeKey`).
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { UserEvent } from "@testing-library/user-event";
import { describe, expect, it, onTestFinished, vi } from "vitest";

import type { ApiKey, KeyList } from "@/lib/api/client";
import { clockAt } from "@/tests/clock";
import {
  account,
  asListed,
  fakeApi,
  GUEST,
  json,
  keyList,
  later,
  listedKey,
  madeKey,
  problem,
} from "@/tests/fake-api";
import { narrowWindow, open } from "@/tests/open";

const SESSION = "POST /api/session/guest";
const LOG_IN = "POST /api/session";
const LOG_OUT = "DELETE /api/session";
const KEYS = "GET /api/keys";
const MAKE = "POST /api/keys";
const REVOKE = "DELETE /api/keys/*";

const ADA = account();
const GOOD_PASSWORD = "correct horse battery";

const done = () => new Response(null, { status: 204 });

// Three keys, in the API's order: an order that sorting them by name or by when they
// were made, either way, would change.
const MANGO = listedKey({
  id: "01900000-0000-7000-8000-0000000000a1",
  name: "Mango",
  created_at: "2026-11-10T09:30:00Z",
});
const APPLE = listedKey({
  id: "01900000-0000-7000-8000-0000000000a2",
  name: "Apple",
  created_at: "2025-03-02T08:00:00Z",
  last_used_at: "2026-11-10T11:15:00Z",
});
const ZEBRA = listedKey({
  id: "01900000-0000-7000-8000-0000000000a3",
  name: "Zebra",
  created_at: "2026-06-01T10:00:00Z",
  last_used_at: "2026-11-09T18:00:00Z",
});
const THREE = [MANGO, APPLE, ZEBRA];

/** The fake API of an account whose keys are read as `lists` say, one after the other,
 * the last one again after that. Without them, as the fake API reads them. */
function asAda(...lists: KeyList[]) {
  const api = fakeApi().on(SESSION, () => json(200, ADA));
  return lists.length === 0
    ? api
    : api.on(KEYS, ...lists.map((list) => () => json(200, list)));
}

/** The section of the account's place that holds its keys. */
function section(): Promise<HTMLElement> {
  return screen.findByRole("region", { name: "API keys" });
}

function heading(): HTMLElement {
  return screen.getByRole("heading", { name: "API keys" });
}

/** The box that shows a key just made. */
function newKeyBox(name: string): Promise<HTMLTextAreaElement> {
  return screen.findByRole<HTMLTextAreaElement>("textbox", {
    name: `Your new key, “${name}”`,
  });
}

/** The list of the account's keys, by name, in the order shown. */
function namesListed(): string[] {
  const list = screen.queryByRole("list", { name: "Your API keys" });
  return list === null
    ? []
    : within(list)
        .getAllByRole("listitem")
        .map((item) => item.querySelector("p")?.textContent ?? "");
}

/** Name a key and make it, as a person does. */
async function make(user: UserEvent, name: string): Promise<void> {
  await user.type(await screen.findByLabelText("Name"), name);
  await user.click(screen.getByRole("button", { name: "Make a key" }));
}

/** Everything this browser keeps for the page, by name and value. */
function stored(): string[] {
  return [window.localStorage, window.sessionStorage].flatMap((storage) =>
    Array.from({ length: storage.length }, (_, at) => {
      const name = storage.key(at) ?? "";
      return `${name}=${storage.getItem(name) ?? ""}`;
    }),
  );
}

/** Take the clipboard away, as at a plain http address other than localhost, for the
 * rest of the test. After `open()`, which puts a stand-in for it in place. */
function withoutClipboard(): void {
  const found = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  Object.defineProperty(navigator, "clipboard", {
    value: undefined,
    configurable: true,
  });
  onTestFinished(() => {
    if (found !== undefined) {
      Object.defineProperty(navigator, "clipboard", found);
    }
  });
}

describe("where the keys are", () => {
  it("isn't there for a guest, who asks nothing of the keys", async () => {
    const api = fakeApi();

    open("/account");

    await screen.findByText(/You are using Actio as a guest/);
    expect(
      screen.queryByRole("heading", { name: "API keys" }),
    ).not.toBeInTheDocument();
    expect(api.to(KEYS)).toHaveLength(0);
  });

  it("isn't there, nor are the keys asked for, before the page knows whose session it has", async () => {
    const session = later(() => json(200, ADA));
    const api = fakeApi().on(SESSION, session.answer);

    open("/account");

    await waitFor(() => {
      expect(api.to(SESSION)).toHaveLength(1);
    });
    expect(screen.getByText("Starting…")).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "API keys" }),
    ).not.toBeInTheDocument();
    expect(api.to(KEYS)).toHaveLength(0);

    session.release();

    expect(await section()).toBeInTheDocument();
    await waitFor(() => {
      expect(api.to(KEYS)).toHaveLength(1);
    });
  });

  it("stands last in the account's place, after Log out", async () => {
    asAda();

    open("/account");

    const keys = await section();
    const place = screen.getByRole("region", { name: "Your account" });
    expect(place.lastElementChild).toBe(keys);
    const logOut = screen.getByRole("region", { name: "Log out" });
    expect(
      logOut.compareDocumentPosition(keys) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("says that it reads the keys until they are read", async () => {
    const reading = later(() => json(200, keyList([], 2)));
    fakeApi()
      .on(SESSION, () => json(200, ADA))
      .on(KEYS, reading.answer);

    open("/account");

    const keys = await section();
    expect(within(keys).getByRole("status")).toHaveTextContent(
      "Reading your keys…",
    );
    // What the plan allows is the API's to say.
    expect(
      within(keys).getByText(/An API key lets a program/),
    ).toHaveTextContent(
      /^An API key lets a program use Actio as this account\.$/,
    );
    expect(screen.queryByLabelText("Name")).not.toBeInTheDocument();

    reading.release();

    expect(await screen.findByLabelText("Name")).toBeInTheDocument();
    expect(screen.queryByText("Reading your keys…")).not.toBeInTheDocument();
  });
});

describe("what the plan allows", () => {
  it("says so where it allows none, and offers no form", async () => {
    asAda(keyList([], 0));

    open("/account");

    const keys = await section();
    expect(
      await within(keys).findByText(/Your plan allows none/),
    ).toHaveTextContent(
      "An API key lets a program use Actio as this account. Your plan allows none.",
    );
    expect(within(keys).queryByLabelText("Name")).not.toBeInTheDocument();
    expect(
      within(keys).queryByRole("button", { name: "Make a key" }),
    ).not.toBeInTheDocument();
    expect(within(keys).queryByRole("list")).not.toBeInTheDocument();
    // Nothing stands where the form would: no word of revoking a key to make one.
    expect(within(keys).queryByText(/to make another/)).not.toBeInTheDocument();
  });

  it("lists the keys it allows no more, each to be revoked, and says that they are refused", async () => {
    const api = asAda(keyList([MANGO, APPLE], 0), keyList([APPLE], 0)).on(
      REVOKE,
      done,
    );
    const user = open("/account");

    const keys = await section();
    expect(
      await within(keys).findByText(/Your plan allows none now/),
    ).toHaveTextContent(
      "An API key lets a program use Actio as this account. Your plan allows none now: the keys below are refused until it does.",
    );
    expect(namesListed()).toEqual(["Mango", "Apple"]);
    expect(
      screen.getByRole("button", { name: "Revoke “Apple”" }),
    ).toBeInTheDocument();
    expect(within(keys).queryByLabelText("Name")).not.toBeInTheDocument();
    // Revoking one makes room for none: nothing says that it would.
    expect(within(keys).queryByText(/to make another/)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Revoke “Mango”" }));
    await user.click(screen.getByRole("button", { name: "Revoke" }));

    await waitFor(() => {
      expect(namesListed()).toEqual(["Apple"]);
    });
    expect(api.to(REVOKE)[0]?.path).toBe(`/api/keys/${MANGO.id}`);
  });

  it("offers the form where it allows two, and there are none yet", async () => {
    asAda(keyList([], 2));

    open("/account");

    const keys = await section();
    const box = await within(keys).findByLabelText("Name");
    expect(
      within(keys).getByText(/An API key lets a program/),
    ).toHaveTextContent(
      "An API key lets a program use Actio as this account. Your plan allows 2 at once.",
    );
    expect(box).toHaveAccessibleDescription(
      "What to call the key, so that you can tell it from your others.",
    );
    expect(
      within(keys).getByRole("button", { name: "Make a key" }),
    ).toBeInTheDocument();
    expect(within(keys).queryByRole("list")).not.toBeInTheDocument();
  });

  it.each([
    [
      "as many keys as it allows",
      keyList([MANGO, APPLE], 2),
      "You have as many keys as your plan allows. Revoke one to make another.",
    ],
    [
      "more keys than it allows",
      keyList([MANGO, APPLE], 1),
      "You have more keys than your plan allows. Revoke some to make another.",
    ],
  ])(
    "says what to do, in the form's place, with %s",
    async (_, list, words) => {
      asAda(list);

      open("/account");

      expect(await screen.findByText(words)).toBeInTheDocument();
      expect(screen.queryByLabelText("Name")).not.toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "Make a key" }),
      ).not.toBeInTheDocument();
      expect(namesListed()).toEqual(["Mango", "Apple"]);
    },
  );
});

describe("the list", () => {
  it("says of each key its name, its prefix, when it was made and when it was last used, in the API's order", async () => {
    clockAt(); // noon, on the day MANGO was made and APPLE last used
    asAda(keyList(THREE, 3));

    open("/account");

    const list = await screen.findByRole("list", { name: "Your API keys" });
    expect(namesListed()).toEqual(["Mango", "Apple", "Zebra"]);
    const [mango, apple, zebra] = within(list).getAllByRole("listitem");
    if (mango === undefined || apple === undefined || zebra === undefined) {
      throw new Error("three rows");
    }
    // Between a time and its "AM" the browser puts a space that doesn't break.
    expect(within(mango).getByText(`${MANGO.prefix}…`)).toBeInTheDocument();
    expect(within(mango).getByText(/^Made today at 9:30\sAM$/)).toBeVisible();
    expect(within(mango).getByText("Never used")).toBeInTheDocument();
    expect(within(apple).getByText(`${APPLE.prefix}…`)).toBeInTheDocument();
    expect(within(apple).getByText("Made on Mar 2, 2025")).toBeInTheDocument();
    expect(
      within(apple).getByText(/^Last used today at 11:15\sAM$/),
    ).toBeInTheDocument();
    expect(within(zebra).getByText("Made on Jun 1, 2026")).toBeInTheDocument();
    expect(
      within(zebra).getByText("Last used on Nov 9, 2026"),
    ).toBeInTheDocument();
  });

  it("names each key's button for a screen reader by the key", async () => {
    asAda(keyList(THREE, 3));

    open("/account");

    const revoke = await screen.findByRole("button", {
      name: "Revoke “Zebra”",
    });
    expect(revoke).toHaveTextContent(/^Revoke$/);
  });
});

describe("making a key", () => {
  it("sends its name without the spaces around it, and shows the whole key once, selected, with the keyboard on it", async () => {
    const made = madeKey({ name: "Deploy script" });
    const api = asAda(keyList([], 2), keyList([asListed(made)], 2)).on(
      MAKE,
      () => json(201, made),
    );
    const user = open("/account");

    await make(user, "  Deploy script  ");

    const box = await newKeyBox("Deploy script");
    expect(api.to(MAKE)).toHaveLength(1);
    expect(api.to(MAKE)[0]?.body).toEqual({ name: "Deploy script" });
    expect(box).toHaveValue(made.key);
    expect(box).toHaveAttribute("readonly");
    expect(box).toHaveAccessibleDescription(
      "Copy it now, and keep it as you keep a password: it isn’t shown again.",
    );
    expect(box).toHaveFocus();
    expect([box.selectionStart, box.selectionEnd]).toEqual([
      0,
      made.key.length,
    ]);
    // The browser is told to leave it alone.
    expect(box).toHaveAttribute("spellcheck", "false");
    expect(box).toHaveAttribute("autocomplete", "off");
    expect(box).toHaveAttribute("autocorrect", "off");
    expect(box).toHaveAttribute("autocapitalize", "off");
    // The key stands in the form's place, and the keys are read again: there it is,
    // by its prefix.
    expect(screen.queryByLabelText("Name")).not.toBeInTheDocument();
    await waitFor(() => {
      expect(namesListed()).toEqual(["Deploy script"]);
    });
    expect(api.to(KEYS)).toHaveLength(2);
    expect(screen.getByText(`${made.prefix}…`)).toBeInTheDocument();
  });

  it("shows the key before the keys are read again, and lists nothing that reading doesn't say", async () => {
    const made = madeKey({ name: "Deploy script" });
    // The reading after the making takes its time, and lists no key: only the plan's
    // number is another, by which the test sees that its answer has been drawn.
    const reading = later(() => json(200, keyList([], 3)));
    const api = fakeApi()
      .on(SESSION, () => json(200, ADA))
      .on(KEYS, () => json(200, keyList([], 2)), reading.answer)
      .on(MAKE, () => json(201, made));
    const user = open("/account");

    await make(user, "Deploy script");

    // The key is the answer's, and doesn't wait for the reading; no row is put in
    // for it meanwhile.
    expect(await newKeyBox("Deploy script")).toHaveValue(made.key);
    await waitFor(() => {
      expect(api.to(KEYS)).toHaveLength(2);
    });
    expect(screen.getByText(/Your plan allows 2 at once/)).toBeVisible();
    expect(namesListed()).toEqual([]);

    reading.release();

    expect(await screen.findByText(/Your plan allows 3 at once/)).toBeVisible();
    expect(namesListed()).toEqual([]);
    expect(
      screen.queryByRole("list", { name: "Your API keys" }),
    ).not.toBeInTheDocument();
    expect(await newKeyBox("Deploy script")).toHaveValue(made.key);
  });

  it("keeps the key in its box alone, and nowhere at all after Done", async () => {
    const made = madeKey({ name: "Deploy script" });
    asAda(keyList([], 2), keyList([asListed(made)], 2)).on(MAKE, () =>
      json(201, made),
    );
    const user = open("/account");
    await make(user, "Deploy script");
    await newKeyBox("Deploy script");
    await waitFor(() => {
      expect(namesListed()).toEqual(["Deploy script"]);
    });

    expect(stored().join("\n")).not.toContain(made.key);
    expect(window.location.href).not.toContain(made.key);
    expect(document.title).not.toContain(made.key);
    const attributes = Array.from(document.querySelectorAll("*")).flatMap(
      (element) =>
        Array.from(element.attributes, (attribute) => attribute.value),
    );
    expect(attributes.join("\n")).not.toContain(made.key);
    expect(screen.getAllByDisplayValue(made.key)).toHaveLength(1);
    // Once in all of the page as it is written: the box's own text.
    expect(document.documentElement.outerHTML.split(made.key)).toHaveLength(2);

    await user.click(screen.getByRole("button", { name: "Done" }));

    expect(screen.queryByDisplayValue(made.key)).not.toBeInTheDocument();
    expect(document.documentElement.outerHTML).not.toContain(made.key);
    expect(stored().join("\n")).not.toContain(made.key);
  });

  it("puts the keyboard on the name's box after Done", async () => {
    const made = madeKey({ name: "Deploy script" });
    asAda(keyList([], 2), keyList([asListed(made)], 2)).on(MAKE, () =>
      json(201, made),
    );
    const user = open("/account");
    await make(user, "Deploy script");
    await newKeyBox("Deploy script");
    await waitFor(() => {
      expect(namesListed()).toEqual(["Deploy script"]);
    });

    await user.click(screen.getByRole("button", { name: "Done" }));

    const box = screen.getByLabelText("Name");
    expect(box).toHaveFocus();
    expect(box).toHaveValue(""); // the name went with the key
  });

  it("puts the keyboard on the heading after Done, where the keys read meanwhile reach the plan's number", async () => {
    const made = madeKey({ name: "Deploy script" });
    asAda(keyList([], 1), keyList([asListed(made)], 1)).on(MAKE, () =>
      json(201, made),
    );
    const user = open("/account");
    await make(user, "Deploy script");
    await newKeyBox("Deploy script");
    await waitFor(() => {
      expect(namesListed()).toEqual(["Deploy script"]);
    });

    await user.click(screen.getByRole("button", { name: "Done" }));

    expect(
      screen.getByText(
        "You have as many keys as your plan allows. Revoke one to make another.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Name")).not.toBeInTheDocument();
    expect(heading()).toHaveFocus();
  });

  it("doesn't show a key again once its place was left and come back to", async () => {
    const made = madeKey({ name: "Deploy script" });
    asAda(keyList([], 2), keyList([asListed(made)], 2)).on(MAKE, () =>
      json(201, made),
    );
    const user = open("/account");
    await make(user, "Deploy script");
    await newKeyBox("Deploy script");

    await user.click(screen.getByRole("button", { name: "New chat" }));
    await screen.findByLabelText("Message");
    await user.click(
      within(screen.getByRole("banner")).getByRole("link", {
        name: `Account: ${ADA.email ?? ""}`,
      }),
    );

    expect(await screen.findByLabelText("Name")).toBeInTheDocument();
    expect(
      screen.queryByRole("textbox", { name: /Your new key/ }),
    ).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue(made.key)).not.toBeInTheDocument();
    await waitFor(() => {
      expect(namesListed()).toEqual(["Deploy script"]);
    });
  });

  it("shows the key all the same when the keys can't be read after it", async () => {
    const made = madeKey({ name: "Deploy script" });
    asAda(keyList([], 2))
      .on(
        KEYS,
        () => json(200, keyList([], 2)),
        () => {
          throw new TypeError("fetch failed");
        },
      )
      .on(MAKE, () => json(201, made));
    const user = open("/account");

    await make(user, "Deploy script");

    expect(
      await screen.findByText(/Actio's server can't be reached/),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeVisible();
    expect(await newKeyBox("Deploy script")).toHaveValue(made.key);
  });
});

describe("copying a key", () => {
  async function aKeyShown(): Promise<{
    user: UserEvent;
    key: string;
    box: HTMLTextAreaElement;
  }> {
    const made = madeKey({ name: "Deploy script" });
    asAda(keyList([], 2), keyList([asListed(made)], 2)).on(MAKE, () =>
      json(201, made),
    );
    const user = open("/account");
    await make(user, "Deploy script");
    const box = await newKeyBox("Deploy script");
    // The keyboard and the selection elsewhere, so that the test sees where "Copy"
    // puts them.
    box.setSelectionRange(0, 0);
    return { user, key: made.key, box };
  }

  it("puts the key on the clipboard, says so, and leaves the keyboard on the button", async () => {
    const { user, key } = await aKeyShown();
    const copy = screen.getByRole("button", { name: "Copy" });

    await user.click(copy);

    expect(await screen.findByText("Copied.")).toHaveAttribute(
      "role",
      "status",
    );
    expect(await navigator.clipboard.readText()).toBe(key);
    expect(copy).toHaveFocus();
  });

  it("puts the keyboard on the button itself, where a click puts it nowhere", async () => {
    const { key, box } = await aKeyShown();
    const copy = screen.getByRole("button", { name: "Copy" });
    expect(box).toHaveFocus();

    // As WebKit clicks a button: the keyboard stays where it was.
    fireEvent.click(copy);

    expect(await screen.findByText("Copied.")).toBeInTheDocument();
    expect(await navigator.clipboard.readText()).toBe(key);
    expect(copy).toHaveFocus();
  });

  it.each([
    [
      "without a clipboard",
      () => {
        withoutClipboard();
      },
    ],
    [
      "with a clipboard that refuses",
      () => {
        vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(
          new DOMException("Write permission denied.", "NotAllowedError"),
        );
      },
    ],
  ])(
    "selects the key for the keyboard to copy %s, and says so",
    async (_, clipboard) => {
      const { user, key, box } = await aKeyShown();
      clipboard();
      // The line is there before anything is said in it.
      const line = within(await section()).getByRole("status");
      expect(line).toBeEmptyDOMElement();

      await user.click(screen.getByRole("button", { name: "Copy" }));

      await waitFor(() => {
        expect(line).toHaveTextContent(
          "It couldn’t be copied for you. It is selected: copy it with your keyboard.",
        );
      });
      expect(box).toHaveFocus();
      expect([box.selectionStart, box.selectionEnd]).toEqual([0, key.length]);
    },
  );
});

describe("a name that won't do", () => {
  it.each([
    ["nothing", ""],
    ["spaces alone", "   "],
  ])(
    "asks for a name when it is %s, and sends nothing",
    async (_, typedName) => {
      const api = asAda(keyList([], 2));
      const user = open("/account");
      const box = await screen.findByLabelText("Name");
      if (typedName !== "") {
        await user.type(box, typedName);
      }

      await user.click(screen.getByRole("button", { name: "Make a key" }));

      expect(box).toHaveAttribute("aria-invalid", "true");
      expect(box).toHaveAccessibleDescription(
        expect.stringContaining("Give the key a name."),
      );
      expect(box).toHaveFocus();
      expect(api.to(MAKE)).toHaveLength(0);
    },
  );

  it("takes no more than the API does, and is no password's or address's", async () => {
    asAda(keyList([], 2));

    open("/account");

    const box = await screen.findByLabelText("Name");
    expect(box).toHaveAttribute("maxlength", "64");
    expect(box).toHaveAttribute("autocomplete", "off");
  });

  it.each([
    [
      "a name it doesn't take",
      () =>
        problem(422, "The request doesn't have the expected form.", {
          errors: [
            {
              where: "body.name",
              problem: "String should have at most 64 characters",
            },
          ],
        }),
      "Use 1 to 64 characters.",
    ],
    [
      "as many keys as the plan allows",
      () =>
        problem(
          409,
          'You have 2 API keys, as many as your plan, "Free", allows. Revoke one to make another.',
        ),
      'You have 2 API keys, as many as your plan, "Free", allows. Revoke one to make another.',
    ],
    [
      "a plan that allows none",
      () => problem(403, 'Your plan, "Free", allows no API keys.'),
      'Your plan, "Free", allows no API keys.',
    ],
  ])(
    "says under the box when the API refuses %s, and reads the keys again",
    async (_, refusal, words) => {
      const api = asAda(keyList([], 2)).on(MAKE, refusal);
      const user = open("/account");

      await make(user, "Deploy script");

      const box = screen.getByLabelText("Name");
      await waitFor(() => {
        expect(box).toHaveAccessibleDescription(expect.stringContaining(words));
      });
      expect(box).toHaveAttribute("aria-invalid", "true");
      expect(box).toHaveFocus();
      // Under the box, and nowhere else.
      expect(screen.getAllByText(words)).toHaveLength(1);
      await waitFor(() => {
        expect(api.to(KEYS)).toHaveLength(2);
      });
      expect(box).toHaveValue("Deploy script");
    },
  );

  it("puts the keyboard on the heading when the form gives way under it", async () => {
    // Refused, as the account has as many keys as its plan allows, made in another
    // window: the keys read again say so, and the form gives way to what to do.
    const elsewhere = listedKey({ name: "Made elsewhere" });
    asAda(keyList([], 1), keyList([elsewhere], 1)).on(MAKE, () =>
      problem(
        409,
        'You have 1 API key, as many as your plan, "Free", allows. Revoke one to make another.',
      ),
    );
    const user = open("/account");

    await make(user, "Deploy script");

    expect(
      await screen.findByText(
        "You have as many keys as your plan allows. Revoke one to make another.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Name")).not.toBeInTheDocument();
    expect(heading()).toHaveFocus();
    expect(namesListed()).toEqual(["Made elsewhere"]);
  });

  it("puts the keyboard on the heading when the form gives way to nothing", async () => {
    // Refused, as the account was taken off its plan meanwhile: the keys read again
    // say that it allows none, and nothing stands where the form was.
    asAda(keyList([], 2), keyList([], 0)).on(MAKE, () =>
      problem(403, 'Your plan, "Free", allows no API keys.'),
    );
    const user = open("/account");

    await make(user, "Deploy script");

    expect(await screen.findByText(/Your plan allows none\./)).toBeVisible();
    expect(screen.queryByLabelText("Name")).not.toBeInTheDocument();
    expect(heading()).toHaveFocus();
  });

  it("leaves the keyboard where it is when the form gives way while it is elsewhere", async () => {
    // The keyboard was in the form, and then went to the header. Another window made
    // the plan's last key meanwhile, and the keys read again say so.
    const elsewhere = listedKey({ name: "Made elsewhere" });
    const reading = later(() => json(200, keyList([elsewhere], 1)));
    asAda()
      .on(KEYS, () => json(200, keyList([], 1)), reading.answer)
      .on(MAKE, () =>
        problem(
          409,
          'You have 1 API key, as many as your plan, "Free", allows. Revoke one to make another.',
        ),
      );
    const user = open("/account");
    await make(user, "Deploy script");
    await waitFor(() => {
      expect(screen.getByLabelText("Name")).toHaveFocus();
    });
    const newChat = screen.getByRole("button", { name: "New chat" });
    newChat.focus();

    reading.release();

    expect(
      await screen.findByText(
        "You have as many keys as your plan allows. Revoke one to make another.",
      ),
    ).toBeInTheDocument();
    expect(newChat).toHaveFocus();
  });
});

describe("making a key that goes wrong", () => {
  it("is sent once, however often the button is pressed meanwhile", async () => {
    const made = madeKey({ name: "Deploy script" });
    const answer = later(() => json(201, made));
    const api = asAda(keyList([], 2)).on(MAKE, answer.answer);
    const user = open("/account");

    await make(user, "Deploy script");
    const busy = await screen.findByRole("button", { name: "Making…" });
    expect(busy).toBeDisabled();
    await user.click(busy);
    await user.type(screen.getByLabelText("Name"), "{Enter}");

    expect(api.to(MAKE)).toHaveLength(1);
    answer.release();
    expect(await newKeyBox("Deploy script")).toHaveValue(made.key);
    expect(api.to(MAKE)).toHaveLength(1);
  });

  it("says a failure that is no refusal of the name, and reads the keys again all the same", async () => {
    const api = asAda(keyList([], 2)).on(MAKE, () => {
      throw new TypeError("fetch failed");
    });
    const user = open("/account");

    await make(user, "Deploy script");

    expect(
      await screen.findByText(/Actio's server can't be reached/),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(api.to(KEYS)).toHaveLength(2);
    });
    const box = screen.getByLabelText("Name");
    expect(box).not.toHaveAttribute("aria-invalid");
    expect(
      screen.getByRole("button", { name: "Make a key" }),
    ).not.toBeDisabled();
    // The name is still there, with the keyboard on it: to be sent again.
    expect(box).toHaveValue("Deploy script");
    expect(box).toHaveFocus();
  });

  it("starts a new session when this one turns out to have ended", async () => {
    const ended = () =>
      problem(
        401,
        "This needs a session. Start one with POST /api/session/guest.",
      );
    const api = fakeApi()
      .on(
        SESSION,
        () => json(200, ADA),
        () => json(201, GUEST),
      )
      .on(KEYS, () => json(200, keyList([], 2)), ended)
      .on(MAKE, ended);
    const user = open("/account");

    await make(user, "Deploy script");

    expect(await screen.findByText("Your session has ended.")).toBeVisible();
    await waitFor(() => {
      expect(api.to(KEYS)).toHaveLength(2);
    });
    // One notice, whatever else ended with the session.
    expect(screen.getAllByText("Your session has ended.")).toHaveLength(1);
    await user.click(
      screen.getByRole("button", { name: "Start a new session" }),
    );

    expect(
      await within(screen.getByRole("banner")).findByRole("link", {
        name: "Log in",
      }),
    ).toBeInTheDocument();
    expect(window.location.pathname).toBe("/");
    expect(api.to(SESSION)).toHaveLength(2);
  });
});

describe("revoking a key", () => {
  it("asks first, and takes no for an answer, from Cancel and from Escape", async () => {
    const api = asAda(keyList(THREE, 3)).on(REVOKE, done);
    const user = open("/account");
    const revoke = await screen.findByRole("button", {
      name: "Revoke “Apple”",
    });

    await user.click(revoke);

    const question = screen.getByRole("group", { name: "Revoke “Apple”?" });
    expect(question).toHaveTextContent(
      "AppleRevoke this key? Whatever uses it is refused from then on, and it can’t be brought back.",
    );
    const cancel = within(question).getByRole("button", { name: "Cancel" });
    expect(cancel).toHaveFocus();
    await user.click(cancel);

    expect(
      screen.queryByRole("group", { name: "Revoke “Apple”?" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Revoke “Apple”" }),
    ).toHaveFocus();

    await user.click(screen.getByRole("button", { name: "Revoke “Apple”" }));
    await user.keyboard("{Escape}");

    expect(
      screen.queryByRole("group", { name: "Revoke “Apple”?" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Revoke “Apple”" }),
    ).toHaveFocus();
    expect(api.to(REVOKE)).toHaveLength(0);
    expect(namesListed()).toEqual(["Mango", "Apple", "Zebra"]);
  });

  it("keeps Escape to itself: the list of conversations over a narrow window stays", async () => {
    narrowWindow();
    asAda(keyList(THREE, 3));
    const user = open("/account");
    await user.click(
      await screen.findByRole("button", { name: "Conversations" }),
    );
    await screen.findByRole("navigation", { name: "Conversations" });
    await user.click(screen.getByRole("button", { name: "Revoke “Apple”" }));

    await user.keyboard("{Escape}");

    expect(
      screen.queryByRole("group", { name: "Revoke “Apple”?" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("navigation", { name: "Conversations" }),
    ).toBeInTheDocument();
  });

  it("revokes it once, keeps its row until the keys read again say it is gone, then puts the keyboard on the next key's", async () => {
    const reading = later(() => json(200, keyList([MANGO, ZEBRA], 3)));
    const api = fakeApi()
      .on(SESSION, () => json(200, ADA))
      .on(KEYS, () => json(200, keyList(THREE, 3)), reading.answer)
      .on(REVOKE, done);
    const user = open("/account");
    await user.click(
      await screen.findByRole("button", { name: "Revoke “Apple”" }),
    );

    await user.click(screen.getByRole("button", { name: "Revoke" }));

    await waitFor(() => {
      expect(api.to(KEYS)).toHaveLength(2);
    });
    expect(api.to(REVOKE)).toHaveLength(1);
    expect(api.to(REVOKE)[0]?.path).toBe(`/api/keys/${APPLE.id}`);
    // Revoked on the server; the page shows it gone once the list says so.
    expect(namesListed()).toEqual(["Mango", "Apple", "Zebra"]);
    expect(screen.getByRole("button", { name: "Revoking…" })).toBeDisabled();

    reading.release();

    await waitFor(() => {
      expect(namesListed()).toEqual(["Mango", "Zebra"]);
    });
    expect(
      screen.getByRole("button", { name: "Revoke “Zebra”" }),
    ).toHaveFocus();
    expect(api.to(REVOKE)).toHaveLength(1);
  });

  it("puts the keyboard on the key before when the last one goes, and on the heading when none is left", async () => {
    const api = fakeApi().on(SESSION, () => json(200, ADA));
    const left = (): ApiKey[] =>
      [MANGO, APPLE].filter(
        (key) => !api.to(REVOKE).some((call) => call.path.endsWith(key.id)),
      );
    api.on(KEYS, () => json(200, keyList(left(), 2))).on(REVOKE, done);
    const user = open("/account");

    await user.click(
      await screen.findByRole("button", { name: "Revoke “Apple”" }),
    );
    await user.click(screen.getByRole("button", { name: "Revoke" }));

    await waitFor(() => {
      expect(namesListed()).toEqual(["Mango"]);
    });
    expect(
      screen.getByRole("button", { name: "Revoke “Mango”" }),
    ).toHaveFocus();

    await user.click(screen.getByRole("button", { name: "Revoke “Mango”" }));
    await user.click(screen.getByRole("button", { name: "Revoke" }));

    await waitFor(() => {
      expect(namesListed()).toEqual([]);
    });
    expect(heading()).toHaveFocus();
  });

  it("takes a key that was revoked already for one revoked now, and says nothing of it", async () => {
    const api = asAda(keyList(THREE, 3), keyList([MANGO, ZEBRA], 3)).on(
      REVOKE,
      () => problem(404, "You have no API key with this id."),
    );
    const user = open("/account");
    await user.click(
      await screen.findByRole("button", { name: "Revoke “Apple”" }),
    );

    await user.click(screen.getByRole("button", { name: "Revoke" }));

    await waitFor(() => {
      expect(namesListed()).toEqual(["Mango", "Zebra"]);
    });
    expect(api.to(KEYS)).toHaveLength(2);
    expect(
      screen.queryByText("You have no API key with this id."),
    ).not.toBeInTheDocument();
    expect(
      within(await section()).queryByRole("status"),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Revoke “Zebra”" }),
    ).toHaveFocus();
  });

  it("says nothing of a key that was revoked already when the keys can't be read after it either", async () => {
    asAda(keyList(THREE, 3))
      .on(
        KEYS,
        () => json(200, keyList(THREE, 3)),
        () => {
          throw new TypeError("fetch failed");
        },
      )
      .on(REVOKE, () => problem(404, "You have no API key with this id."));
    const user = open("/account");
    await user.click(
      await screen.findByRole("button", { name: "Revoke “Apple”" }),
    );

    await user.click(screen.getByRole("button", { name: "Revoke" }));

    // What went wrong is the reading: the key is as revoked as was asked for.
    expect(
      await screen.findByText(/Actio's server can't be reached/),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("You have no API key with this id."),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeVisible();
  });

  it("puts the keyboard on the heading when the key after it has a question of its own open", async () => {
    asAda(keyList([MANGO, APPLE], 3), keyList([APPLE], 3)).on(REVOKE, done);
    const user = open("/account");
    await user.click(
      await screen.findByRole("button", { name: "Revoke “Apple”" }),
    );
    await user.click(screen.getByRole("button", { name: "Revoke “Mango”" }));

    await user.click(
      within(screen.getByRole("group", { name: "Revoke “Mango”?" })).getByRole(
        "button",
        { name: "Revoke" },
      ),
    );

    await waitFor(() => {
      expect(namesListed()).toEqual(["Apple"]);
    });
    // Apple's own button isn't on the page while its question is.
    expect(
      screen.getByRole("group", { name: "Revoke “Apple”?" }),
    ).toBeInTheDocument();
    expect(heading()).toHaveFocus();
  });

  it("says what went wrong otherwise, and keeps the row, with the keyboard back on its button", async () => {
    const api = asAda(keyList(THREE, 3)).on(REVOKE, () => {
      throw new TypeError("fetch failed");
    });
    const user = open("/account");
    await user.click(
      await screen.findByRole("button", { name: "Revoke “Apple”" }),
    );

    await user.click(screen.getByRole("button", { name: "Revoke" }));

    expect(
      await screen.findByText(/Actio's server can't be reached/),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("group", { name: "Revoke “Apple”?" }),
    ).not.toBeInTheDocument();
    expect(namesListed()).toEqual(["Mango", "Apple", "Zebra"]);
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Revoke “Apple”" }),
      ).toHaveFocus();
    });
    expect(api.to(REVOKE)).toHaveLength(1);
    // Read again all the same: the key is there, by what the API says now.
    expect(api.to(KEYS)).toHaveLength(2);
  });

  it("takes a key for revoked when its answer never arrived and the keys read again don't list it", async () => {
    const api = asAda(keyList(THREE, 3), keyList([MANGO, ZEBRA], 3)).on(
      REVOKE,
      () => {
        throw new TypeError("fetch failed");
      },
    );
    const user = open("/account");
    await user.click(
      await screen.findByRole("button", { name: "Revoke “Apple”" }),
    );

    await user.click(screen.getByRole("button", { name: "Revoke" }));

    await waitFor(() => {
      expect(namesListed()).toEqual(["Mango", "Zebra"]);
    });
    expect(api.to(KEYS)).toHaveLength(2);
    // Gone is what was asked for: nothing is said of the answer that was lost.
    expect(
      screen.queryByText(/Actio's server can't be reached/),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Revoke “Zebra”" }),
    ).toHaveFocus();
  });

  it("keeps what was read before, and the row, when the keys can't be read after it", async () => {
    const api = asAda(keyList(THREE, 3))
      .on(
        KEYS,
        () => json(200, keyList(THREE, 3)),
        () => {
          throw new TypeError("fetch failed");
        },
      )
      .on(REVOKE, done);
    const user = open("/account");
    await user.click(
      await screen.findByRole("button", { name: "Revoke “Apple”" }),
    );

    await user.click(screen.getByRole("button", { name: "Revoke" }));

    expect(
      await screen.findByText(/Actio's server can't be reached/),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeVisible();
    // Revoked on the server, it is still listed: the page shows what it read last.
    expect(api.to(REVOKE)).toHaveLength(1);
    expect(namesListed()).toEqual(["Mango", "Apple", "Zebra"]);
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Revoke “Apple”" }),
      ).toHaveFocus();
    });
  });
});

describe("reading the keys", () => {
  it("says when they couldn't be read, and reads them again at Try again", async () => {
    const api = fakeApi()
      .on(SESSION, () => json(200, ADA))
      .on(
        KEYS,
        () => {
          throw new TypeError("fetch failed");
        },
        () => json(200, keyList([MANGO], 2)),
      );
    const user = open("/account");

    expect(
      await screen.findByText(/Actio's server can't be reached/),
    ).toBeInTheDocument();
    expect(screen.queryByText("Reading your keys…")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));

    await waitFor(() => {
      expect(namesListed()).toEqual(["Mango"]);
    });
    expect(
      screen.queryByText(/Actio's server can't be reached/),
    ).not.toBeInTheDocument();
    expect(api.to(KEYS)).toHaveLength(2);
    // The button went with its notice: the keyboard is on the heading.
    expect(heading()).toHaveFocus();
  });

  it("reads them anew for the next session, and shows nothing of the last one's meanwhile", async () => {
    // Meanwhile the account got another key, made in another browser: what the page
    // read during the earlier session is of no use.
    const reading = later(() => json(200, keyList([MANGO, APPLE], 2)));
    const api = fakeApi()
      .on(
        SESSION,
        () => json(200, ADA),
        () => json(201, GUEST),
      )
      .on(KEYS, () => json(200, keyList([MANGO], 2)), reading.answer)
      .on(LOG_OUT, done)
      .on(LOG_IN, () => json(200, ADA));
    const user = open("/account");
    await waitFor(() => {
      expect(namesListed()).toEqual(["Mango"]);
    });
    await user.click(screen.getByRole("button", { name: "Log out" }));
    const header = screen.getByRole("banner");
    await user.click(
      await within(header).findByRole("link", { name: "Log in" }),
    );
    await user.type(
      await screen.findByLabelText("Email address"),
      "ada@example.com",
    );
    await user.type(
      screen.getByLabelText("Password"),
      `${GOOD_PASSWORD}{Enter}`,
    );
    await user.click(
      await within(header).findByRole("link", {
        name: `Account: ${ADA.email ?? ""}`,
      }),
    );

    const keys = await section();
    expect(within(keys).getByRole("status")).toHaveTextContent(
      "Reading your keys…",
    );
    expect(namesListed()).toEqual([]);

    reading.release();

    await waitFor(() => {
      expect(namesListed()).toEqual(["Mango", "Apple"]);
    });
    expect(api.to(KEYS)).toHaveLength(2);
  });
});
