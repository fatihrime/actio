// Signing up, logging in, the account and logging out, driven as a person would:
// through the whole page, against the fake API (tests/fake-api.ts).
import { screen, waitFor, within } from "@testing-library/react";
import type { UserEvent } from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import {
  account,
  answer,
  conversation,
  fakeApi,
  GUEST,
  json,
  listed,
  problem,
  question,
  streamed,
  summary,
} from "@/tests/fake-api";
import { ask, open } from "@/tests/open";
import { recorded, RECORDED } from "@/tests/recorded";

const SESSION = "POST /api/session/guest";
const SIGN_UP = "POST /api/account";
const LOG_IN = "POST /api/session";
const LOG_OUT = "DELETE /api/session";
const LOG_OUT_EVERYWHERE = "DELETE /api/sessions";
const PASSWORD = "PUT /api/account/password";
const LIST = "GET /api/conversations";
const CONVERSATION = "GET /api/conversations/*";
const TURNS = "POST /api/conversations/*/turns";

const SKY = RECORDED.conversation;
const TYRES = "01900000-0000-7000-8000-0000000000c2";
const ADA = account();
const GOOD_PASSWORD = "correct horse battery";

const done = () => new Response(null, { status: 204 });

function storedSky(): Response {
  return new Response(recorded("conversation.json"), {
    headers: { "content-type": "application/json" },
  });
}

/** The top of the page, where it says whose session this is. */
function header(): HTMLElement {
  return screen.getByRole("banner");
}

/** The middle of the page: the chat, or one of the forms. */
function main(): HTMLElement {
  return screen.getByRole("main");
}

/** The link in the header that leads to an account's own page. */
function accountLink(): HTMLElement {
  return within(header()).getByRole("link", {
    name: `Account: ${ADA.email ?? ""}`,
  });
}

async function fillIn(
  user: UserEvent,
  boxes: Record<string, string>,
): Promise<void> {
  for (const [label, text] of Object.entries(boxes)) {
    if (text !== "") {
      await user.type(await screen.findByLabelText(label), text);
    }
  }
}

describe("signing up", () => {
  it("makes the guest an account, and goes back to where the chat was left", async () => {
    const api = fakeApi()
      .on(LIST, () => json(200, listed([summary(SKY, "Why is the sky blue?")])))
      .on(CONVERSATION, storedSky)
      // The same account as the guest was: it only has an address now.
      .on(SIGN_UP, () => json(201, account({ id: GUEST.id })));
    const user = open(`/c/${SKY}`);
    await screen.findByText("Echo: Why is the sky blue?");

    await user.click(within(header()).getByRole("link", { name: "Sign up" }));

    expect(window.location.pathname).toBe("/signup");
    expect(
      screen.getByRole("heading", { name: "Create an account" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/What you did as a guest stays yours/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("article")).not.toBeInTheDocument();

    await fillIn(user, {
      "Email address": "Ada@Example.com",
      Password: GOOD_PASSWORD,
    });
    await user.click(screen.getByRole("button", { name: "Sign up" }));

    expect(
      await screen.findByText("Echo: Why is the sky blue?"),
    ).toBeInTheDocument();
    expect(window.location.pathname).toBe(`/c/${SKY}`);
    expect(api.to(SIGN_UP)[0]?.body).toEqual({
      email: "Ada@Example.com",
      password: GOOD_PASSWORD,
    });
    expect(accountLink()).toHaveAttribute("href", "/account");
    expect(
      within(header()).queryByRole("link", { name: "Log in" }),
    ).not.toBeInTheDocument();
    // Nothing reminds an account that guests don't last.
    expect(screen.queryByText(/You are a guest/)).not.toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Why is the sky blue?" }),
    ).toBeInTheDocument();
  });

  it.each([
    [
      "no address",
      "",
      GOOD_PASSWORD,
      "Email address",
      "Enter your email address.",
    ],
    [
      "something that is no address",
      "ada",
      GOOD_PASSWORD,
      "Email address",
      "This doesn’t look like an email address.",
    ],
    [
      "a password that is too short",
      "ada@example.com",
      "too short",
      "Password",
      "Use at least 10 characters.",
    ],
  ])(
    "says what is wrong with %s, and sends nothing",
    async (_, email, password, box, message) => {
      const api = fakeApi();
      const user = open("/signup");

      await fillIn(user, { "Email address": email, Password: password });
      await user.click(await screen.findByRole("button", { name: "Sign up" }));

      expect(screen.getByText(message)).toBeInTheDocument();
      expect(api.to(SIGN_UP)).toHaveLength(0);
      // Said with the box, and the keyboard is on it.
      const wrong = screen.getByLabelText(box);
      expect(wrong).toHaveAttribute("aria-invalid", "true");
      expect(wrong).toHaveAccessibleDescription(
        expect.stringContaining(message),
      );
      expect(wrong).toHaveFocus();
    },
  );

  it("says so under the address when it has an account already", async () => {
    fakeApi().on(SIGN_UP, () =>
      problem(409, "An account with this email address already exists."),
    );
    const user = open("/signup");

    await fillIn(user, {
      "Email address": "ada@example.com",
      Password: GOOD_PASSWORD,
    });
    await user.click(screen.getByRole("button", { name: "Sign up" }));

    const address = screen.getByLabelText("Email address");
    await waitFor(() => {
      expect(address).toHaveAccessibleDescription(
        "An account with this email address already exists.",
      );
    });
    expect(address).toHaveFocus();
    expect(window.location.pathname).toBe("/signup");
    // What was typed is still there, to change the address or to log in instead.
    expect(screen.getByLabelText("Password")).toHaveValue(GOOD_PASSWORD);
    expect(screen.getByRole("button", { name: "Sign up" })).toBeEnabled();
  });

  it("takes the API's word for an address it doesn't accept, in the page's own words", async () => {
    fakeApi().on(SIGN_UP, () =>
      problem(422, "The request doesn't have the expected form.", {
        errors: [
          {
            where: "body.email",
            problem:
              "value is not a valid email address: The part after the @-sign is not valid.",
          },
        ],
      }),
    );
    const user = open("/signup");

    await fillIn(user, { "Email address": "ada@nas", Password: GOOD_PASSWORD });
    await user.click(screen.getByRole("button", { name: "Sign up" }));

    expect(
      await screen.findByText(
        "The server doesn’t take this as an email address.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/@-sign/)).not.toBeInTheDocument();
    expect(screen.getByLabelText("Email address")).toHaveFocus();
  });

  it("says how long to wait after too many attempts, in the API's words", async () => {
    fakeApi().on(SIGN_UP, () =>
      problem(
        429,
        "Too many sign-up attempts from your network. Try again in 12 minutes.",
        { retry_after: 720 },
      ),
    );
    const user = open("/signup");

    await fillIn(user, {
      "Email address": "ada@example.com",
      Password: GOOD_PASSWORD,
    });
    await user.click(screen.getByRole("button", { name: "Sign up" }));

    expect(
      await within(main()).findByText(
        "Too many sign-up attempts from your network. Try again in 12 minutes.",
      ),
    ).toBeInTheDocument();
  });

  it("is sent once, however often the button is pressed meanwhile", async () => {
    let answerIt: (response: Response) => void = () => undefined;
    const api = fakeApi().on(
      SIGN_UP,
      () =>
        new Promise<Response>((resolve) => {
          answerIt = resolve;
        }),
    );
    const user = open("/signup");
    await fillIn(user, {
      "Email address": "ada@example.com",
      Password: GOOD_PASSWORD,
    });

    await user.click(screen.getByRole("button", { name: "Sign up" }));
    const busy = await screen.findByRole("button", { name: "Signing up…" });
    expect(busy).toBeDisabled();
    await user.click(busy);
    await user.keyboard("{Enter}");

    expect(api.to(SIGN_UP)).toHaveLength(1);
    answerIt(json(201, account({ id: GUEST.id })));
    expect(await screen.findByLabelText("Message")).toBeInTheDocument();
  });

  it("shows the password while it is typed, when asked", async () => {
    fakeApi();
    const user = open("/signup");
    const box = await screen.findByLabelText("Password");
    const show = screen.getByRole("button", { name: "Show the password" });
    expect(box).toHaveAttribute("type", "password");
    expect(show).toHaveAttribute("aria-pressed", "false");

    await user.click(show);

    expect(box).toHaveAttribute("type", "text");
    expect(show).toHaveAttribute("aria-pressed", "true");
    // For a password manager it is a new password either way.
    expect(box).toHaveAttribute("autocomplete", "new-password");
  });
});

describe("logging in", () => {
  it("makes the session the account's, and reads the list that is the account's", async () => {
    const asGuest = summary(SKY, "Why is the sky blue?");
    const before = summary(TYRES, "Flat tyres");
    const api = fakeApi();
    api
      // A guest's list; then the account's, with what the guest had started.
      .on(LIST, () =>
        json(
          200,
          listed(api.to(LOG_IN).length === 0 ? [asGuest] : [asGuest, before]),
        ),
      )
      .on(LOG_IN, () => json(200, ADA));
    const user = open();
    await screen.findByRole("link", { name: "Why is the sky blue?" });

    await user.click(within(header()).getByRole("link", { name: "Log in" }));

    expect(window.location.pathname).toBe("/login");
    expect(
      screen.getByText(
        "The conversations you started as a guest come with you.",
      ),
    ).toBeInTheDocument();
    await fillIn(user, {
      "Email address": "ada@example.com",
      Password: `${GOOD_PASSWORD}{Enter}`,
    });

    expect(
      await screen.findByRole("link", { name: "Flat tyres" }),
    ).toBeInTheDocument();
    expect(api.to(LOG_IN)[0]?.body).toEqual({
      email: "ada@example.com",
      password: GOOD_PASSWORD,
    });
    expect(window.location.pathname).toBe("/");
    expect(screen.getByLabelText("Message")).toBeInTheDocument();
    expect(accountLink()).toBeInTheDocument();
  });

  it("says that the two don't match in the API's words, and keeps what was typed", async () => {
    const api = fakeApi().on(LOG_IN, () =>
      problem(401, "Wrong email address or password."),
    );
    const user = open("/login");

    await fillIn(user, {
      "Email address": "ada@example.com",
      Password: "not the password",
    });
    await user.click(screen.getByRole("button", { name: "Log in" }));

    expect(
      await screen.findByText("Wrong email address or password."),
    ).toBeInTheDocument();
    // A refused login is not a session that ended.
    expect(
      screen.queryByText("Your session has ended."),
    ).not.toBeInTheDocument();
    expect(screen.getByLabelText("Email address")).toHaveValue(
      "ada@example.com",
    );
    expect(screen.getByLabelText("Password")).toHaveValue("not the password");
    expect(screen.getByLabelText("Password")).toHaveFocus();
    expect(window.location.pathname).toBe("/login");
    expect(api.to(LOG_IN)).toHaveLength(1);
  });

  it("asks for both before it sends anything, and for nothing about their form", async () => {
    const api = fakeApi().on(LOG_IN, () =>
      problem(401, "Wrong email address or password."),
    );
    const user = open("/login");

    await user.click(await screen.findByRole("button", { name: "Log in" }));

    expect(screen.getByText("Enter your email address.")).toBeInTheDocument();
    expect(screen.getByText("Enter your password.")).toBeInTheDocument();
    expect(api.to(LOG_IN)).toHaveLength(0);

    // Whatever is typed goes to the API: only it may say that a login is wrong.
    await fillIn(user, { "Email address": "ada", Password: "x" });
    await user.click(screen.getByRole("button", { name: "Log in" }));

    await screen.findByText("Wrong email address or password.");
    expect(api.to(LOG_IN)[0]?.body).toEqual({ email: "ada", password: "x" });
  });

  it("says how long to wait after too many attempts", async () => {
    fakeApi().on(LOG_IN, () =>
      problem(429, "Too many login attempts. Try again in 40 seconds.", {
        retry_after: 40,
      }),
    );
    const user = open("/login");

    await fillIn(user, {
      "Email address": "ada@example.com",
      Password: `${GOOD_PASSWORD}{Enter}`,
    });

    expect(
      await screen.findByText(
        "Too many login attempts. Try again in 40 seconds.",
      ),
    ).toBeInTheDocument();
  });

  it("can be reached by someone with an account when no guest session could be started", async () => {
    const api = fakeApi()
      .on(SESSION, () =>
        problem(
          429,
          "Too many new guests from your network. Try again in 2 minutes.",
          { retry_after: 120 },
        ),
      )
      .on(LOG_IN, () => json(200, ADA));
    const user = open();
    await screen.findByText(/Too many new guests/);

    await user.click(within(header()).getByRole("link", { name: "Log in" }));
    await fillIn(user, {
      "Email address": "ada@example.com",
      Password: `${GOOD_PASSWORD}{Enter}`,
    });

    expect(await screen.findByLabelText("Message")).toBeInTheDocument();
    expect(accountLink()).toBeInTheDocument();
    expect(api.to(SESSION)).toHaveLength(1); // no guest was needed for it
  });

  it.each(["/login", "/signup"])(
    "promises nothing at %s to a guest who has started nothing",
    async (address) => {
      fakeApi();

      open(address);

      await screen.findByLabelText("Password");
      await screen.findByText(/No conversations yet/);
      expect(screen.queryByText(/come with you/)).not.toBeInTheDocument();
      expect(screen.queryByText(/stays yours/)).not.toBeInTheDocument();
    },
  );

  it("waits until it is known whose session this is", async () => {
    // A login sent while a guest session is still being started could be overtaken
    // by it, and leave the browser with the guest's cookie.
    let start: (response: Response) => void = () => undefined;
    const api = fakeApi().on(
      SESSION,
      () =>
        new Promise<Response>((resolve) => {
          start = resolve;
        }),
    );

    open("/login");

    await waitFor(() => {
      expect(api.to(SESSION)).toHaveLength(1);
    });
    expect(screen.getByText("Starting…")).toBeInTheDocument();
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();

    start(json(201, GUEST));

    expect(await screen.findByLabelText("Password")).toBeInTheDocument();
  });

  it("leads to signing up and back, without a step in the browser's history", async () => {
    fakeApi();
    const user = open("/login");
    await screen.findByLabelText("Password");
    const steps = window.history.length;

    await user.click(within(main()).getByRole("link", { name: "Sign up" }));

    expect(window.location.pathname).toBe("/signup");
    expect(
      screen.getByRole("heading", { name: "Create an account" }),
    ).toBeInTheDocument();

    await user.click(within(main()).getByRole("link", { name: "Log in" }));

    expect(window.location.pathname).toBe("/login");
    expect(window.history.length).toBe(steps);
  });

  it.each(["/login", "/signup"])(
    "tells an account at %s that it is logged in already",
    async (address) => {
      const api = fakeApi().on(SESSION, () => json(200, ADA));

      open(address);

      expect(await screen.findByText(/You are logged in as/)).toHaveTextContent(
        "You are logged in as ada@example.com.",
      );
      expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
      expect(
        within(main()).getByRole("link", { name: "your account" }),
      ).toHaveAttribute("href", "/account");
      expect(api.to(LOG_IN)).toHaveLength(0);
    },
  );
});

describe("the account", () => {
  const asAda = () => fakeApi().on(SESSION, () => json(200, ADA));

  it("says whose it is", async () => {
    asAda();

    open("/account");

    expect(
      await screen.findByRole("heading", { name: "Your account" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/You are logged in as/)).toHaveTextContent(
      "You are logged in as ada@example.com.",
    );
  });

  it("tells a guest how to keep what they have", async () => {
    fakeApi();
    const user = open("/account");

    expect(
      await screen.findByText(/You are using Actio as a guest/),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Current password")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Log out" }),
    ).not.toBeInTheDocument();

    await user.click(within(main()).getByRole("link", { name: "Sign up" }));

    expect(window.location.pathname).toBe("/signup");
  });

  it("changes the password, and says that every other browser was logged out", async () => {
    const api = asAda().on(PASSWORD, done);
    const user = open("/account");

    await fillIn(user, {
      "Current password": GOOD_PASSWORD,
      "New password": "staple battery horse",
    });
    await user.click(screen.getByRole("button", { name: "Change password" }));

    expect(
      await screen.findByText(
        "Your password is changed. Every other browser was logged out.",
      ),
    ).toBeInTheDocument();
    expect(api.to(PASSWORD)[0]?.body).toEqual({
      current_password: GOOD_PASSWORD,
      new_password: "staple battery horse",
    });
    // Neither stays on the page.
    expect(screen.getByLabelText("Current password")).toHaveValue("");
    expect(screen.getByLabelText("New password")).toHaveValue("");
    expect(window.location.pathname).toBe("/account");
  });

  it("says so under the current password when it is wrong", async () => {
    asAda().on(PASSWORD, () => problem(403, "The current password is wrong."));
    const user = open("/account");

    await fillIn(user, {
      "Current password": "not the password",
      "New password": "staple battery horse",
    });
    await user.click(screen.getByRole("button", { name: "Change password" }));

    const current = screen.getByLabelText("Current password");
    await waitFor(() => {
      expect(current).toHaveAccessibleDescription(
        "The current password is wrong.",
      );
    });
    expect(current).toHaveFocus();
    expect(screen.queryByText(/Your password is changed/)).toBeNull();
  });

  it("holds a new password to the API's length before it sends anything", async () => {
    const api = asAda();
    const user = open("/account");

    await fillIn(user, {
      "Current password": GOOD_PASSWORD,
      "New password": "too short",
    });
    await user.click(screen.getByRole("button", { name: "Change password" }));

    expect(screen.getByText("Use at least 10 characters.")).toBeInTheDocument();
    expect(screen.getByLabelText("New password")).toHaveFocus();
    expect(api.to(PASSWORD)).toHaveLength(0);
  });

  it("starts a new session when this one turns out to have ended", async () => {
    const api = fakeApi()
      .on(
        SESSION,
        () => json(200, ADA),
        () => json(201, GUEST),
      )
      .on(PASSWORD, () =>
        problem(
          401,
          "This needs a session. Start one with POST /api/session/guest.",
        ),
      );
    const user = open("/account");
    await fillIn(user, {
      "Current password": GOOD_PASSWORD,
      "New password": "staple battery horse",
    });
    await user.click(screen.getByRole("button", { name: "Change password" }));

    await user.click(
      await screen.findByRole("button", { name: "Start a new session" }),
    );

    expect(
      await within(header()).findByRole("link", { name: "Log in" }),
    ).toBeInTheDocument();
    expect(window.location.pathname).toBe("/");
    expect(api.to(SESSION)).toHaveLength(2);
  });
});

describe("logging out", () => {
  /** An account's session at first, and a new guest's once that has ended. */
  function asAdaThenGuest() {
    const api = fakeApi();
    return api
      .on(
        SESSION,
        () => json(200, ADA),
        () => json(201, GUEST),
      )
      .on(LIST, () =>
        json(
          200,
          listed(
            // Asked by the account, there is a conversation; by the guest, none.
            api.to(SESSION).length === 1 ? [summary(TYRES, "Flat tyres")] : [],
          ),
        ),
      );
  }

  it("ends the session, and begins a guest's with nothing of the account in it", async () => {
    const api = asAdaThenGuest().on(LOG_OUT, done);
    const user = open("/account");
    await screen.findByRole("link", { name: "Flat tyres" });

    await user.click(screen.getByRole("button", { name: "Log out" }));

    expect(
      await within(header()).findByRole("link", { name: "Log in" }),
    ).toBeInTheDocument();
    expect(api.to(LOG_OUT)).toHaveLength(1);
    expect(api.to(SESSION)).toHaveLength(2);
    expect(window.location.pathname).toBe("/");
    expect(await screen.findByText(/No conversations yet/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Flat tyres" })).toBeNull();
    expect(screen.queryByText(/ada@example.com/)).not.toBeInTheDocument();
    expect(screen.getByLabelText("Message")).toBeInTheDocument();
  });

  it("shows nothing of the account while the next session is being started", async () => {
    let start: (response: Response) => void = () => undefined;
    const api = fakeApi();
    api
      .on(
        SESSION,
        () => json(200, ADA),
        () =>
          new Promise<Response>((resolve) => {
            start = resolve;
          }),
      )
      .on(LIST, () =>
        json(
          200,
          listed(
            api.to(LOG_OUT).length === 0 ? [summary(TYRES, "Flat tyres")] : [],
          ),
        ),
      )
      .on(LOG_OUT, done);
    const user = open("/account");
    await screen.findByRole("link", { name: "Flat tyres" });
    const asked = api.to(LIST).length;

    await user.click(screen.getByRole("button", { name: "Log out" }));

    // The cookie is gone, and the next session isn't there yet: nobody is logged in.
    await waitFor(() => {
      expect(api.to(SESSION)).toHaveLength(2);
    });
    expect(screen.getByText("Starting…")).toBeInTheDocument();
    expect(screen.queryByText(/ada@example.com/)).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Flat tyres" })).toBeNull();
    expect(screen.queryByRole("button", { name: "New chat" })).toBeNull();
    expect(api.to(LIST)).toHaveLength(asked); // and nothing is asked for as the account

    start(json(201, GUEST));

    expect(await screen.findByLabelText("Message")).toBeInTheDocument();
    expect(await screen.findByText(/No conversations yet/)).toBeInTheDocument();
  });

  it("shows nothing remembered from before when the same account logs in again", async () => {
    // Meanwhile the account has got another conversation: the one a guest brought
    // along. What the page read during the earlier session is of no use.
    const api = fakeApi();
    api
      .on(
        SESSION,
        () => json(200, ADA),
        () => json(201, GUEST),
      )
      .on(LIST, () =>
        json(
          200,
          listed(
            api.to(LOG_IN).length === 0
              ? api.to(LOG_OUT).length === 0
                ? [summary(TYRES, "Flat tyres")]
                : []
              : [
                  summary(SKY, "Why is the sky blue?"),
                  summary(TYRES, "Flat tyres"),
                ],
          ),
        ),
      )
      .on(LOG_OUT, done)
      .on(LOG_IN, () => json(200, ADA));
    const user = open("/account");
    await screen.findByRole("link", { name: "Flat tyres" });
    await user.click(screen.getByRole("button", { name: "Log out" }));
    await screen.findByText(/No conversations yet/);

    await user.click(within(header()).getByRole("link", { name: "Log in" }));
    await fillIn(user, {
      "Email address": "ada@example.com",
      Password: `${GOOD_PASSWORD}{Enter}`,
    });

    expect(
      await screen.findByRole("link", { name: "Why is the sky blue?" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Flat tyres" }),
    ).toBeInTheDocument();
  });

  it("can end the account's sessions in every browser at once", async () => {
    const api = asAdaThenGuest().on(LOG_OUT_EVERYWHERE, done);
    const user = open("/account");

    await user.click(
      await screen.findByRole("button", { name: "Log out everywhere" }),
    );

    expect(
      await within(header()).findByRole("link", { name: "Log in" }),
    ).toBeInTheDocument();
    expect(api.to(LOG_OUT_EVERYWHERE)).toHaveLength(1);
    expect(api.to(LOG_OUT)).toHaveLength(0);
  });

  it("counts a session that had ended already as logged out", async () => {
    asAdaThenGuest().on(LOG_OUT_EVERYWHERE, () =>
      problem(
        401,
        "This needs a session. Start one with POST /api/session/guest.",
      ),
    );
    const user = open("/account");

    await user.click(
      await screen.findByRole("button", { name: "Log out everywhere" }),
    );

    expect(
      await within(header()).findByRole("link", { name: "Log in" }),
    ).toBeInTheDocument();
    expect(window.location.pathname).toBe("/");
  });

  it("stays logged in, and says why, when the server couldn't be told", async () => {
    const api = asAdaThenGuest().on(LOG_OUT, () => {
      throw new TypeError("fetch failed");
    });
    const user = open("/account");

    await user.click(await screen.findByRole("button", { name: "Log out" }));

    expect(
      await within(main()).findByText(/Actio's server can't be reached/),
    ).toBeInTheDocument();
    expect(window.location.pathname).toBe("/account");
    expect(accountLink()).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Log out" })).toBeEnabled();
    expect(api.to(SESSION)).toHaveLength(1);
  });
});

describe("whose session it is, around the chat", () => {
  it("offers a guest both ways in, and tells them how long the list lasts", async () => {
    fakeApi();
    const user = open();
    await screen.findByLabelText("Message");

    expect(
      within(header()).getByRole("link", { name: "Log in" }),
    ).toHaveAttribute("href", "/login");
    expect(
      within(header()).getByRole("link", { name: "Sign up" }),
    ).toHaveAttribute("href", "/signup");
    const note = screen.getByText(/You are a guest/);
    expect(note).toHaveTextContent(
      "You are a guest: these conversations are kept for a day. Sign up to keep them.",
    );

    await user.click(within(note).getByRole("link", { name: "Sign up" }));

    expect(window.location.pathname).toBe("/signup");
    // No link leads to where one already is.
    expect(
      within(header()).queryByRole("link", { name: "Sign up" }),
    ).not.toBeInTheDocument();
    expect(
      within(header()).getByRole("link", { name: "Log in" }),
    ).toBeInTheDocument();
  });

  it("names the place in the browser's tab", async () => {
    fakeApi();
    const user = open("/login");

    await waitFor(() => {
      expect(document.title).toBe("Log in · Actio");
    });

    await user.click(await screen.findByRole("button", { name: "New chat" }));

    await waitFor(() => {
      expect(document.title).toBe("Actio");
    });
    expect(window.location.pathname).toBe("/");
  });

  it("reads a conversation that began here again, after a form was on the page", async () => {
    const api = fakeApi();
    const asked = (): string =>
      (api.to(TURNS)[0]?.body as { message?: { id: string } }).message?.id ??
      "";
    api
      .on(TURNS, () => streamed("answer.sse"))
      .on(CONVERSATION, (call) =>
        json(
          200,
          conversation(call.path.split("/")[3] ?? "", [
            question(asked(), "Why is the sky blue?"),
            answer(RECORDED.answer, "Echo: Why is the sky blue?", asked()),
          ]),
        ),
      );
    const user = open();
    await ask(user, "Why is the sky blue?");
    await screen.findByText("Echo: Why is the sky blue?");
    const address = window.location.pathname;

    await user.click(within(header()).getByRole("link", { name: "Log in" }));
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
    window.history.back();

    // The form took its place on the page: it is read from the server, not shown
    // as an empty conversation under its address.
    expect(
      await screen.findByText("Echo: Why is the sky blue?"),
    ).toBeInTheDocument();
    expect(window.location.pathname).toBe(address);
    expect(api.to(CONVERSATION)).toHaveLength(1);
  });
});
