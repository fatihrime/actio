// Accounts, through the whole stack: a real cookie, a real password check, and what
// the database does with a guest's conversations when the guest signs up or logs in.
import { randomUUID } from "node:crypto";

import { Chat, expect, test } from "./chat";

const PASSWORD = "correct horse battery";

/** An email address nobody else has: every test, and every run, signs up anew. */
function anAddress(): string {
  return `${randomUUID()}@example.com`;
}

test("a guest who signs up keeps what they had, and is still logged in after a reload", async ({
  chat,
  page,
}) => {
  const email = anAddress();
  await chat.open();
  await chat.converse("Something worth keeping.");
  const address = page.url();
  await expect(page.getByText(/You are a guest/)).toBeVisible();

  await chat.header.getByRole("link", { name: "Sign up" }).click();
  await expect(page).toHaveURL(/\/signup$/);
  await expect(page).toHaveTitle("Sign up · Actio");
  await expect(
    page.getByText(/What you did as a guest stays yours/),
  ).toBeVisible();
  await chat.email.fill(email);
  await chat.password.fill(PASSWORD);
  await chat.main.getByRole("button", { name: "Sign up" }).click();

  // Back where the chat was left, as the same account with an address now.
  await expect(page).toHaveURL(address);
  await expect(chat.answers).toHaveText("Echo: Something worth keeping.");
  await expect(chat.accountLink(email)).toBeVisible();
  await expect(chat.listed).toHaveText(["Something worth keeping."]);
  await expect(page.getByText(/You are a guest/)).toHaveCount(0);

  await page.reload();
  await expect(chat.accountLink(email)).toBeVisible();
  await expect(chat.answers).toHaveText("Echo: Something worth keeping.");
  await expect(chat.listed).toHaveText(["Something worth keeping."]);
});

test("logging out leaves nothing of the account on the page, and logging in brings it back", async ({
  chat,
  page,
}) => {
  const email = anAddress();
  await chat.open();
  await chat.converse("The account's own.");
  const address = page.url();
  await chat.signUp(email, PASSWORD);

  await chat.logOut(email);

  // A guest again, with a guest's empty list, in a new conversation.
  await expect(page).toHaveURL(/\/$/);
  await expect(chat.list).toContainText("No conversations yet");
  await expect(page.getByText("What would you like to know?")).toBeVisible();
  await expect(page.getByText(email)).toHaveCount(0);
  // And the account's conversation is nobody else's to open.
  await page.goto(address);
  await expect(
    page.getByText(/doesn.t exist, or belongs to another session/),
  ).toBeVisible();

  await chat.logIn(email, PASSWORD);

  await expect(chat.accountLink(email)).toBeVisible();
  await expect(chat.listed).toHaveText(["The account's own."]);
  await chat.listed.first().click();
  await expect(chat.answers).toHaveText("Echo: The account's own.");
});

test("what a guest started goes along into the account they log in to", async ({
  chat,
  page,
}) => {
  const email = anAddress();
  await chat.open();
  await chat.converse("Asked with the account.");
  await chat.signUp(email, PASSWORD);
  await chat.logOut(email);

  // Later, in the same browser: someone tries Actio as a guest, then remembers
  // that they have an account.
  await chat.converse("Asked as a guest.");
  const asGuest = page.url();
  await chat.logIn(email, PASSWORD);

  await expect(chat.accountLink(email)).toBeVisible();
  // Back in the conversation they were in, which is the account's now.
  await expect(page).toHaveURL(asGuest);
  await expect(chat.answers).toHaveText("Echo: Asked as a guest.");
  await expect(chat.listed).toHaveText([
    "Asked as a guest.",
    "Asked with the account.",
  ]);

  await page.reload();
  await expect(chat.listed).toHaveText([
    "Asked as a guest.",
    "Asked with the account.",
  ]);
});

test("a wrong password is refused in so many words, and an address signs up only once", async ({
  chat,
  page,
}) => {
  const email = anAddress();
  await chat.open();
  await expect(chat.box).toBeVisible();
  await chat.signUp(email, PASSWORD);
  await chat.logOut(email);

  await chat.logIn(email, "not the password");
  await expect(
    chat.main.getByText("Wrong email address or password."),
  ).toBeVisible();
  await expect(page).toHaveURL(/\/login$/);
  await expect(chat.logInLink).toHaveCount(0); // no link to where one is
  await expect(chat.accountLink(email)).toHaveCount(0);

  // The address is taken: signing up with it again says so, under the address.
  await chat.main.getByRole("link", { name: "Sign up" }).click();
  await chat.email.fill(email.toUpperCase()); // however it is typed
  await chat.password.fill(PASSWORD);
  await chat.main.getByRole("button", { name: "Sign up" }).click();
  await expect(chat.email).toHaveAccessibleDescription(
    "An account with this email address already exists.",
  );

  await chat.main.getByRole("link", { name: "Log in" }).click();
  await chat.email.fill(email);
  await chat.password.fill(PASSWORD);
  await chat.password.press("Enter");
  await expect(chat.accountLink(email)).toBeVisible();
});

test("changing the password logs every other browser out, and only the new one works", async ({
  chat,
  page,
  browser,
}) => {
  const email = anAddress();
  await chat.open();
  await expect(chat.box).toBeVisible();
  await chat.signUp(email, PASSWORD);

  // The same account, logged in from another browser.
  const elsewhere = await browser.newContext();
  const other = new Chat(await elsewhere.newPage());
  await other.open();
  await other.logIn(email, PASSWORD);
  await expect(other.accountLink(email)).toBeVisible();

  await chat.accountLink(email).click();
  await expect(page).toHaveTitle("Your account · Actio");
  // By their whole labels: each box has a "Show the …" button beside it.
  const current = page.getByLabel("Current password", { exact: true });
  await current.fill(PASSWORD);
  await page
    .getByLabel("New password", { exact: true })
    .fill("staple battery horse");
  await page.getByRole("button", { name: "Change password" }).click();
  await expect(
    page.getByText(
      "Your password is changed. Every other browser was logged out.",
    ),
  ).toBeVisible();
  await expect(current).toHaveValue("");

  // This browser stays logged in, with its new cookie.
  await page.reload();
  await expect(chat.accountLink(email)).toBeVisible();

  // The other one is a guest at its next visit, and the old password is no use.
  await other.page.reload();
  await expect(other.logInLink).toBeVisible();
  await expect(other.accountLink(email)).toHaveCount(0);
  await other.logIn(email, PASSWORD);
  await expect(
    other.main.getByText("Wrong email address or password."),
  ).toBeVisible();
  await other.password.fill("staple battery horse");
  await other.password.press("Enter");
  await expect(other.accountLink(email)).toBeVisible();
  await elsewhere.close();
});

test("each of the three places has an address of its own", async ({
  chat,
  page,
}) => {
  // Opened by its address, as from a bookmark: a page of its own to the server.
  await chat.open("/login");
  await expect(page).toHaveTitle("Log in · Actio");
  await expect(chat.password).toBeVisible();

  await chat.open("/signup");
  await expect(page).toHaveTitle("Sign up · Actio");
  await expect(
    page.getByRole("heading", { name: "Create an account" }),
  ).toBeVisible();

  await chat.open("/account");
  await expect(page).toHaveTitle("Your account · Actio");
  await expect(page.getByText(/You are using Actio as a guest/)).toBeVisible();

  // And from there into the chat without loading a page.
  let loads = 0;
  page.on("load", () => loads++);
  await chat.newChat.click();
  await expect(page).toHaveTitle("Actio");
  await expect(chat.box).toBeVisible();
  expect(loads).toBe(0);
});

test.describe("in a narrow window", () => {
  test.use({ viewport: { width: 390, height: 780 } });

  test("the header keeps one way in, and the list leads to the other", async ({
    chat,
    page,
  }) => {
    await chat.open();
    await expect(chat.box).toBeVisible();

    await expect(chat.logInLink).toBeVisible();
    await expect(
      chat.header.getByRole("link", { name: "Sign up" }),
    ).toBeHidden();
    // Nothing in the header is pushed off the screen.
    const newChat = await chat.newChat.boundingBox();
    expect((newChat?.x ?? 0) + (newChat?.width ?? 0)).toBeLessThanOrEqual(390);

    await chat.listButton.click();
    await page
      .getByText(/You are a guest/)
      .getByRole("link", { name: "Sign up" })
      .click();

    await expect(page).toHaveURL(/\/signup$/);
    await expect(chat.list).toBeHidden();
    await expect(chat.password).toBeVisible();
  });
});
