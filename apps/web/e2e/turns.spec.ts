// The ways an answer can end other than well, through the whole stack: stopped by the
// reader, refused by the model's server, broken off half-way. After each, the page
// shows what the server kept, and a reload shows the same.
import { expect, test } from "./chat";

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
