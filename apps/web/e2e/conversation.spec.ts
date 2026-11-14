// A conversation, from a first visit on: asked, answered, kept, and found again.
import { expect, test } from "./chat";

/** The address a conversation gets once the server has it. */
const ITS_ADDRESS = /\/c\/[0-9a-f-]{36}$/;

test("a first visit can ask at once, and reads the answer while it is written", async ({
  chat,
  page,
}) => {
  await chat.open();
  await expect(page.getByText("What would you like to know?")).toBeVisible();
  await expect(page.getByText(/Answered by Fake model/)).toBeVisible();

  await chat.ask(
    "[[pace: 0.2]] Why is the sky blue on a clear day and red in the evening?",
  );

  // The first words are on the page while the rest is still being written. Were the
  // answer held back somewhere on its way and delivered whole, there would be nothing
  // left to stop by now.
  await expect(chat.answers).toContainText("Echo: Why");
  await expect(chat.stop).toBeVisible();

  await chat.answered();
  await expect(chat.answers).toHaveText(
    "Echo: Why is the sky blue on a clear day and red in the evening?",
  );
  await expect(page).toHaveURL(ITS_ADDRESS);
});

test("a conversation is still there after a reload, and goes on from where it was", async ({
  chat,
  page,
}) => {
  await chat.open();
  await chat.ask("Name a bike part.");
  await expect(chat.answers).toHaveText("Echo: Name a bike part.");
  await chat.answered();

  await page.reload();
  await expect(chat.questions).toHaveText("Name a bike part.");
  await expect(chat.answers).toHaveText("Echo: Name a bike part.");

  await chat.ask("And another?");
  await expect(chat.answers).toHaveText([
    "Echo: Name a bike part.",
    "Echo: And another?",
  ]);
  await chat.answered();

  await page.reload();
  await expect(chat.questions).toHaveText([
    "Name a bike part.",
    "And another?",
  ]);
  await expect(chat.answers).toHaveText([
    "Echo: Name a bike part.",
    "Echo: And another?",
  ]);
});

test("New chat starts another conversation, and Back returns to the first", async ({
  chat,
  page,
}) => {
  await chat.open();
  await chat.ask("The first conversation.");
  await expect(chat.answers).toHaveText("Echo: The first conversation.");
  await chat.answered();
  await expect(page).toHaveURL(ITS_ADDRESS);
  const first = page.url();

  await page.getByRole("button", { name: "New chat" }).click();
  await expect(page.getByText("What would you like to know?")).toBeVisible();
  await expect(chat.questions).toHaveCount(0);

  await chat.ask("The second conversation.");
  await expect(chat.answers).toHaveText("Echo: The second conversation.");
  await chat.answered();
  await expect(page).toHaveURL(ITS_ADDRESS);
  expect(page.url()).not.toBe(first);

  await page.goBack();
  await expect(page).toHaveURL(first);
  await expect(chat.answers).toHaveText("Echo: The first conversation.");
});

test("a conversation opens for nobody but the one who had it", async ({
  chat,
  page,
  browser,
}) => {
  await chat.open();
  await chat.ask("Something of mine.");
  await expect(chat.answers).toHaveText("Echo: Something of mine.");
  await chat.answered();
  await expect(page).toHaveURL(ITS_ADDRESS);

  // Another browser: another guest.
  const theirs = await browser.newContext();
  const stranger = await theirs.newPage();
  await stranger.goto(page.url());

  await expect(
    stranger.getByText(/doesn.t exist, or belongs to another session/),
  ).toBeVisible();
  await expect(stranger.getByText("Something of mine.")).toHaveCount(0);
  await theirs.close();
});
