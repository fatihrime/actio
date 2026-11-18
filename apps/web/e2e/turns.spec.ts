// The ways an answer can end other than well, through the whole stack: stopped by the
// reader, refused by the model's server, broken off half-way. After each, the page
// shows what the server kept, and a reload shows the same. And the one thing that
// doesn't end an answer: its reader going away. Whoever comes back reads along.
//
// Then what is refused before an answer begins: one answer more than a guest may have
// written at once, and a request too large for the proxy in front of the API.
import { randomUUID } from "node:crypto";

import { Chat, expect, test } from "./chat";

/** Twenty words: at half a second each, an answer still on its way after a reload
 * or a second window, with seconds to spare. */
const TWENTY =
  "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty";

test("Stop ends an answer, and what was written is kept", async ({
  chat,
  page,
}) => {
  await chat.open();
  await chat.ask(
    "[[pace: 0.2]] one two three four five six seven eight nine ten eleven twelve",
  );
  await expect(chat.answers).toContainText("three");

  await chat.stop.click();

  const stopped = page.getByText(
    "This answer was stopped before it was finished.",
  );
  await expect(stopped).toBeVisible();
  await expect(chat.answers).toContainText("Echo: one two three");
  await expect(chat.answers).not.toContainText("twelve");
  const kept = await chat.answers.innerText();

  // The model was stopped too: nothing more arrives, here or on the server.
  await page.reload();
  await expect(chat.answers).toHaveText(kept);
  await expect(stopped).toBeVisible();
});

test("an answer is written to its end when the page that asked is gone", async ({
  chat,
  page,
}) => {
  await chat.open();
  await chat.ask(`[[pace: 0.5]] ${TWENTY}`);
  await expect(chat.answers).toContainText("three");

  // A reload ends the page, and with it the reading. Nothing stops the answer.
  await page.reload();

  // The page that comes back reads along with it: what was written so far at once,
  // and the rest as it is written.
  await expect(chat.questions).toHaveCount(1);
  await expect(chat.stop).toBeVisible();
  await expect(chat.answers).toContainText("Echo: one two three");
  await expect(chat.answers).not.toContainText("twenty");
  await expect(page.getByText(/still being written/)).toBeHidden();
  await expect(chat.answers).toHaveText(`Echo: ${TWENTY}`, {
    timeout: 20_000,
  });
  await chat.answered();
  await expect(page.getByText(/stopped before it was finished/)).toBeHidden();
});

test("a second window reads along, and Stop there ends the answer in both", async ({
  chat,
  page,
  context,
}) => {
  await chat.open();
  await chat.ask(`[[pace: 0.5]] ${TWENTY}`);
  await expect(chat.answers).toContainText("three");
  await expect(page).toHaveURL(/\/c\//);

  // The same browser, so the same session: another tab at the conversation's address.
  const other = new Chat(await context.newPage());
  const broken: string[] = [];
  other.page.on("pageerror", (error) => broken.push(error.message));
  await other.open(page.url());
  await expect(other.stop).toBeVisible();
  await expect(other.answers).toContainText("Echo: one two three");

  await other.stop.click();

  const stopped = (window: Chat) =>
    window.page.getByText("This answer was stopped before it was finished.");
  await expect(stopped(other)).toBeVisible();
  await expect(stopped(chat)).toBeVisible();
  await expect(other.send).toBeVisible();
  await expect(chat.send).toBeVisible();
  const kept = await other.answers.innerText();
  expect(kept).toMatch(/^Echo: one two three/);
  expect(kept).not.toContain("twenty");
  await expect(chat.answers).toHaveText(kept);

  await page.reload();
  await expect(chat.answers).toHaveText(kept);
  await expect(stopped(chat)).toBeVisible();
  expect(broken, "errors in the second window's scripts").toEqual([]);
});

test("a model that fails is said so, and the question can be tried again or reworded", async ({
  chat,
  page,
}) => {
  const failed = page.getByText(/can't be reached, or is too busy/);
  await chat.open();
  await chat.ask("[[fail: 503]] Hello?");
  await expect(failed).toBeVisible();
  await expect(chat.answers).toHaveCount(0);

  // Try again asks the model for the same question once more. This model fails on
  // it every time.
  const again = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().endsWith("/turns"),
  );
  await page.getByRole("button", { name: "Try again" }).click();
  expect((await again).status()).toBe(200);
  await expect(failed).toBeVisible();
  await expect(chat.questions).toHaveCount(1);

  // A new question takes the place of the one that got no answer.
  await chat.ask("Hello again.");
  await expect(chat.answers).toHaveText("Echo: Hello again.");
  await chat.answered();
  await expect(chat.questions).toHaveText("Hello again.");
  await expect(failed).toBeHidden();

  await page.reload();
  await expect(chat.questions).toHaveText("Hello again.");
  await expect(chat.answers).toHaveText("Echo: Hello again.");
});

test("an answer that breaks off keeps what had arrived", async ({
  chat,
  page,
}) => {
  await chat.open();
  // The model's server sends three words, and then the connection is gone.
  await chat.ask("[[drop: 3]] one two three four five");

  const brokeOff = page.getByText(
    "The answer broke off: the connection to the model was lost.",
  );
  await expect(brokeOff).toBeVisible();
  await expect(chat.answers).toHaveText("Echo: one two");

  await page.reload();
  await expect(chat.answers).toHaveText("Echo: one two");
  await expect(brokeOff).toBeVisible();
});

test("reasoning shows while it is written, then folds away", async ({
  chat,
  page,
}) => {
  await chat.open();
  await page.getByLabel("Think before answering").check();
  await chat.ask(
    "[[think: Blue light scatters most, and red light least.]] [[pace: 0.25]] Why is the sky blue?",
  );
  const thought = chat.answers.locator("details");

  await expect(thought.locator("summary")).toHaveText("Thinking…");
  await expect(thought).toHaveAttribute("open");
  await expect(thought).toContainText("Blue light");

  await expect(chat.answers).toContainText("Echo: Why is the sky blue?");
  await chat.answered();
  await expect(thought.locator("summary")).toHaveText(
    "Thought before answering",
  );
  await expect(thought).not.toHaveAttribute("open");

  // Folded away, and there to read.
  await thought.locator("summary").click();
  await expect(
    thought.getByText("Blue light scatters most, and red light least."),
  ).toBeVisible();
});

test("a third answer at once is refused, and is answered once one of the two is stopped", async ({
  chat,
  page,
}) => {
  // Two answers in two conversations, at a word a second: twenty seconds each, which
  // leaves the rest of the test room to spare. A guest may have two written at once
  // (deploy/e2e/config/plans.toml).
  await chat.open();
  await chat.ask(`[[pace: 1]] First: ${TWENTY}`);
  await expect(chat.answers).toContainText("one");
  await chat.newChat.click();
  await expect(chat.questions).toHaveCount(0);
  await chat.ask(`[[pace: 1]] Second: ${TWENTY}`);
  await expect(chat.answers).toContainText("one");

  await chat.newChat.click();
  await expect(chat.questions).toHaveCount(0);
  await chat.ask("And a third, meanwhile?");

  await expect(
    page.getByText(
      "2 answers are already being written for you, as many as your plan allows at once. Wait for one to finish, or stop one.",
    ),
  ).toBeVisible();
  await expect(chat.box).toHaveValue("And a third, meanwhile?");
  await expect(chat.questions).toHaveCount(0);

  // Stop the second, from its conversation.
  await chat.listed.filter({ hasText: "Second:" }).click();
  await chat.stop.click();
  await expect(
    page.getByText("This answer was stopped before it was finished."),
  ).toBeVisible();

  await chat.converse("And a third, meanwhile?");

  // The first was still being written all the while: the stop made the room.
  await chat.listed.filter({ hasText: "First:" }).click();
  await expect(chat.stop).toBeVisible();
  await expect(chat.answers).not.toContainText("twenty");
  await chat.stop.click();
  await chat.answered();
});

test("a request too large for the proxy is refused there, and a long one reaches the API", async ({
  chat,
  page,
}) => {
  await chat.open();
  await expect(chat.box).toBeVisible(); // there is a session now
  // As the page asks, with this browser's session.
  const ask = (characters: number) =>
    page.request.post(`/api/conversations/${randomUUID()}/turns`, {
      data: {
        message: {
          id: randomUUID(),
          parts: [{ type: "text", text: "a".repeat(characters) }],
        },
        parent_id: null,
        thinking: false,
      },
    });

  // Two million characters: more than the proxy lets through in one request.
  const tooLarge = await ask(2_000_000);
  expect(tooLarge.status()).toBe(413);
  expect(tooLarge.headers()["content-type"]).toBe("application/problem+json");
  expect(((await tooLarge.json()) as { detail: string }).detail).toBe(
    "This is too much to send at once: Actio takes at most 2 MB in one request. Shorten the message, and send it again.",
  );

  // One character more than a question may have: the proxy lets it through, and the
  // API says what is wrong with it.
  const tooLong = await ask(100_001);
  expect(tooLong.status()).toBe(422);
  expect(await tooLong.text()).toContain(
    "a message has at most 100000 characters",
  );
});
