// The list of conversations, through the whole stack: what the server lists, in the
// order it decides, and what renaming and deleting leave behind after a reload.
import { randomUUID } from "node:crypto";

import { expect, test } from "./chat";

test("a conversation is listed once it was asked, and the one continued last is on top", async ({
  chat,
  page,
}) => {
  await chat.open();
  await expect(chat.list).toContainText("No conversations yet");

  await chat.converse("Bikes: how to fix a flat tyre?");
  await expect(chat.listed).toHaveText(["Bikes: how to fix a flat tyre?"]);
  await expect(chat.listed.first()).toHaveAttribute("aria-current", "page");

  await chat.converse("Bread without yeast?");
  await expect(chat.listed).toHaveText([
    "Bread without yeast?",
    "Bikes: how to fix a flat tyre?",
  ]);

  // Opening one changes the address and what is shown. No page is loaded.
  let loads = 0;
  page.on("load", () => loads++);
  await chat.listed.filter({ hasText: "Bikes" }).click();
  await expect(chat.answers).toHaveText("Echo: Bikes: how to fix a flat tyre?");
  await expect(chat.listed.filter({ hasText: "Bikes" })).toHaveAttribute(
    "aria-current",
    "page",
  );
  await expect(page).toHaveURL(
    (await chat.listed.filter({ hasText: "Bikes" }).getAttribute("href")) ?? "",
  );

  // Back to the one this page started, and has shown something else since: it is
  // read from the server, like any other.
  await chat.listed.filter({ hasText: "Bread" }).click();
  await expect(chat.questions).toHaveText("Bread without yeast?");
  await expect(chat.answers).toHaveText("Echo: Bread without yeast?");
  expect(loads).toBe(0);

  // Going on with the older one moves it to the top.
  await chat.listed.filter({ hasText: "Bikes" }).click();
  await expect(chat.answers).toHaveCount(1);
  await chat.ask("And a broken chain?");
  await expect(chat.answers).toHaveCount(2);
  await chat.answered();
  await expect(chat.listed).toHaveText([
    "Bikes: how to fix a flat tyre?",
    "Bread without yeast?",
  ]);

  await page.reload();
  await expect(chat.listed).toHaveText([
    "Bikes: how to fix a flat tyre?",
    "Bread without yeast?",
  ]);
  await expect(chat.listed.first()).toHaveAttribute("aria-current", "page");
});

test("a conversation's title, written by a model after its first answer, shows by itself", async ({
  chat,
  page,
}) => {
  // The model that names conversations here is the fake's quiet one: it says what the
  // question tells it to, and the answer's model leaves that out of its echo. The
  // answer takes its time, so that the first line is seen while no title is asked for.
  const question = "[[pace: 0.2]] [[say: Flat tyres]] How do I fix a puncture?";
  await chat.open();
  await chat.ask(question);

  await expect(chat.stop).toBeVisible();
  await expect(chat.listed).toHaveText([question]);
  await expect(chat.answers).toHaveText("Echo: How do I fix a puncture?");
  await chat.answered();

  // Nothing is touched: the page looks again by itself while the server says that a
  // title is on its way, for 15 seconds. A title takes about one here, but the worker
  // writes one at a time, and the other tests' titles may be ahead of this one: so the
  // test waits as long as the page looks.
  await expect(chat.listed).toHaveText(["Flat tyres"], { timeout: 15_000 });
  await expect(chat.listed.first()).toHaveAttribute("aria-current", "page");

  await page.reload();
  await expect(chat.listed).toHaveText(["Flat tyres"]);
});

test("a title that arrives while its conversation is being renamed leaves the box as it is, and Enter keeps what the box shows", async ({
  chat,
  page,
}) => {
  // In a real browser, because the page's own tests can't vouch for one: a box that
  // nobody has typed in could follow the text it was given to start with, and a title
  // that arrived would then stand in it, under the person's hands. It doesn't: the box
  // is given its text once. The title takes three seconds here, since a question's
  // directives reach the request for its title too: time enough to open the box first.
  const question = "[[delay: 3]] [[say: Flat tyres]] How do I fix a puncture?";
  await chat.open();
  await chat.ask(question);
  await expect(chat.answers).toHaveText("Echo: How do I fix a puncture?", {
    timeout: 15_000,
  });
  await chat.answered();
  // One of the page's own looks at the list will bring the model's title.
  const arrival = page.waitForResponse(
    async (response) =>
      response.request().method() === "GET" &&
      new URL(response.url()).pathname === "/api/conversations" &&
      (
        (await response.json()) as { items: { title: string | null }[] }
      ).items.some((item) => item.title === "Flat tyres"),
    { timeout: 15_000 },
  );

  await chat.listed.first().hover();
  await page.getByRole("button", { name: `Rename “${question}”` }).click();
  const title = page.getByRole("textbox", { name: "Title" });
  await expect(title).toHaveValue(question);
  await arrival;
  // The page has the answer by now, and has drawn what it made of it: twice over.
  await page.evaluate(
    () =>
      new Promise<void>((drawn) => {
        requestAnimationFrame(() => {
          requestAnimationFrame(() => {
            drawn();
          });
        });
      }),
  );

  await expect(title).toHaveValue(question);
  await expect(title).toBeFocused();
  await title.press("Enter");

  // What the box showed is the person's title now: the model's doesn't come back.
  await expect(chat.listed).toHaveText([question]);
  await page.reload();
  await expect(chat.listed).toHaveText([question]);
});

test("a conversation can be renamed, and keeps its place and its new name", async ({
  chat,
  page,
}) => {
  await chat.open();
  await chat.converse("An older one.");
  await chat.converse("Why is the sky blue on a clear day?");
  await chat.listed.filter({ hasText: "An older one." }).hover();

  await page.getByRole("button", { name: "Rename “An older one.”" }).click();
  const title = page.getByRole("textbox", { name: "Title" });
  await expect(title).toBeFocused();
  await expect(title).toHaveValue("An older one.");
  await title.fill("Flat tyres");
  await title.press("Enter");

  // Renaming is not activity: it stays below the newer one.
  await expect(chat.listed).toHaveText([
    "Why is the sky blue on a clear day?",
    "Flat tyres",
  ]);
  await page.reload();
  await expect(chat.listed).toHaveText([
    "Why is the sky blue on a clear day?",
    "Flat tyres",
  ]);
});

test("a deleted conversation is gone: from the list, from the page and at its address", async ({
  chat,
  page,
}) => {
  await chat.open();
  await chat.converse("One to keep.");
  await chat.converse("One to delete.");
  const address = page.url();

  await page.getByRole("button", { name: "Delete “One to delete.”" }).click();
  const asking = page.getByRole("group", { name: "Delete “One to delete.”?" });
  await expect(asking).toContainText("It can’t be brought back.");
  await asking.getByRole("button", { name: "Delete", exact: true }).click();

  // It was the one on the page: a new conversation takes its place.
  await expect(page.getByText("What would you like to know?")).toBeVisible();
  await expect(page).toHaveURL(/\/$/);
  await expect(chat.listed).toHaveText(["One to keep."]);

  await page.goto(address);
  await expect(
    page.getByText(/doesn.t exist, or belongs to another session/),
  ).toBeVisible();
  await expect(chat.listed).toHaveText(["One to keep."]);
});

test("a long list is read a page at a time", async ({ chat, page }) => {
  await chat.open();
  await expect(chat.box).toBeVisible(); // there is a session now
  // More conversations than one page holds, made the way the page makes them.
  for (let number = 1; number <= 31; number++) {
    const made = await page.request.post(
      `/api/conversations/${randomUUID()}/turns`,
      {
        data: {
          message: {
            id: randomUUID(),
            parts: [{ type: "text", text: `Conversation ${String(number)}` }],
          },
          parent_id: null,
          thinking: false,
        },
      },
    );
    expect(made.status()).toBe(200);
  }

  await page.reload();
  await expect(chat.listed).toHaveCount(30);
  await expect(chat.listed.first()).toHaveText("Conversation 31");
  await expect(chat.listed.last()).toHaveText("Conversation 2");

  await page.getByRole("button", { name: "Show older conversations" }).click();

  await expect(chat.listed).toHaveCount(31);
  await expect(chat.listed.last()).toHaveText("Conversation 1");
  await expect(
    page.getByRole("button", { name: "Show older conversations" }),
  ).toHaveCount(0);
});

test("an answer is written on while another conversation is open, and is read along with on coming back", async ({
  chat,
  page,
}) => {
  // Twenty words at half a second each: still on its way when the page is back.
  const twenty =
    "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty";
  await chat.open();
  await chat.converse("Another conversation.");
  await chat.newChat.click();
  await chat.ask(`[[pace: 0.5]] ${twenty}`);
  await expect(chat.answers).toContainText("Echo: one two");
  const slow = chat.listed.filter({ hasText: "pace" });
  await expect(slow).toHaveAttribute("aria-current", "page");

  await chat.listed.filter({ hasText: "Another conversation." }).click();
  await expect(chat.answers).toHaveText("Echo: Another conversation.");

  // Nobody is reading the slow answer now. It is written all the same: whoever
  // comes back reads along with it while it is written, and in the end it is whole.
  await slow.click();
  await expect(chat.stop).toBeVisible();
  await expect(chat.answers).toContainText("Echo: one two");
  await expect(chat.answers).not.toContainText("twenty");
  await expect(chat.answers).toHaveText(`Echo: ${twenty}`, {
    timeout: 20_000,
  });
  await chat.answered();
  await expect(page.getByText(/stopped before it was finished/)).toBeHidden();
  await expect(page.getByText(/still being written/)).toBeHidden();
});

test.describe("in a narrow window", () => {
  test.use({ viewport: { width: 390, height: 780 } });

  test("the list lies over the chat while it is wanted, and the chat has the whole width", async ({
    chat,
    page,
  }) => {
    await chat.open();
    await chat.converse("The first one.");
    await chat.converse("The second one.");

    // Away by itself: the box to write in is as wide as the window allows.
    await expect(chat.list).toBeHidden();
    expect((await chat.box.boundingBox())?.width).toBeGreaterThan(300);

    await chat.listButton.click();
    await expect(chat.listed).toHaveText(["The second one.", "The first one."]);
    // The chat keeps its width underneath.
    expect((await chat.box.boundingBox())?.width).toBeGreaterThan(300);

    await chat.listed.filter({ hasText: "The first one." }).click();
    await expect(chat.list).toBeHidden();
    await expect(chat.answers).toHaveText("Echo: The first one.");

    await chat.listButton.click();
    await expect(chat.list).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(chat.list).toBeHidden();
  });
});
