// Where a system's own few colours take the place of the page's (`forced-colors`, as
// Windows' contrast themes do), every ground on the page is the system's one ground.
// What the page sets apart by its ground alone gets a line around it there, which the
// system colours, and a meter's bar the colour of the words (ADR 0015, revision "with
// forced colours, what stood apart by its ground has a line"). What only a real browser
// shows: what the styles make of it. The browser is told that colours are forced
// (Playwright's `forcedColors`), and the styles are read as it computed them.
//
// The theme's own choice, underlined there, is in scheme.spec.ts.
import type { Locator } from "@playwright/test";

import { expect, test } from "./chat";
import type { Chat } from "./chat";

test.use({ forcedColors: "active" });

/** The width of the line around something: "0px" where it has none. */
function line(part: Locator): Promise<string> {
  return part.evaluate((element) => getComputedStyle(element).borderTopWidth);
}

function ground(part: Locator): Promise<string> {
  return part.evaluate((element) => getComputedStyle(element).backgroundColor);
}

/** The colour of the page's words: the product's name at the top. */
function words(chat: Chat): Promise<string> {
  return chat.title.evaluate((title) => getComputedStyle(title).color);
}

/** A meter's bar, and what it is filled with. */
function barOf(meter: Locator): { bar: Locator; filled: Locator } {
  return { bar: meter, filled: meter.locator("> div") };
}

test("what stood apart by its ground has a line around it, and a meter's bar the colour of the words", async ({
  chat,
  page,
}) => {
  // Two conversations, the second one on the page: its row is the current one.
  await chat.open();
  await chat.ask("Which saddle fits a road bike?");
  await expect(chat.answers).toHaveText("Echo: Which saddle fits a road bike?");
  await chat.answered();
  await chat.converse("And which tyres?");
  await expect(chat.listed).toHaveCount(2);

  const question = chat.questions.locator("> div");
  const current = chat.list.locator('a[aria-current="page"]');
  const other = chat.list.locator("a:not([aria-current])");
  const usage = barOf(chat.meter("Session"));
  const context = barOf(page.getByRole("meter", { name: "Context" }));
  await expect(current).toHaveCount(1);
  await expect(other).toHaveCount(1);
  await expect(context.bar).toBeVisible();

  const lines = async () => ({
    question: await line(question),
    "the current conversation": await line(current),
    "another conversation": await line(other),
    "the usage meter's bar": await line(usage.bar),
    "the context meter's bar": await line(context.bar),
  });
  expect(await lines()).toEqual({
    question: "1px",
    "the current conversation": "1px",
    "another conversation": "0px",
    "the usage meter's bar": "1px",
    "the context meter's bar": "1px",
  });
  // What is a ground and nothing else, a bar's filling and the dot between an
  // answer's figures, is in the colour of the words, on the ground of the page: with
  // a colour of the page's own, the system gives it that ground, and it isn't seen.
  const dot = page
    .getByRole("list", { name: "What this answer took" })
    .locator("span[aria-hidden]")
    .first();
  const inWords = await words(chat);
  for (const { bar, filled } of [usage, context]) {
    expect(await ground(filled)).toBe(inWords);
    expect(await ground(bar)).not.toBe(inWords);
  }
  expect(await ground(dot)).toBe(inWords);

  // The two buttons that are a filled ground and nothing else: without a line they
  // are words, there.
  const askToDelete = async () => {
    await current.hover();
    await page
      .getByRole("button", { name: "Delete “And which tyres?”" })
      .click();
    return page
      .getByRole("group", { name: "Delete “And which tyres?”?" })
      .getByRole("button", { name: "Delete", exact: true });
  };
  expect(await line(await askToDelete())).toBe("1px");
  await page.keyboard.press("Escape");
  await chat.logInLink.click();
  const logIn = chat.main.getByRole("button", { name: "Log in" });
  expect(await line(logIn)).toBe("1px");

  // With the page's own colours, none of these lines is drawn: the grounds say it.
  await page.emulateMedia({ forcedColors: "none" });
  await expect.poll(() => line(logIn)).toBe("0px");
  await page.goBack();
  await expect(context.bar).toBeVisible();
  expect(await lines()).toEqual({
    question: "0px",
    "the current conversation": "0px",
    "another conversation": "0px",
    "the usage meter's bar": "0px",
    "the context meter's bar": "0px",
  });
  const inOwnWords = await words(chat);
  for (const { filled } of [usage, context]) {
    expect(await ground(filled)).not.toBe(inOwnWords);
  }
  expect(await ground(dot)).not.toBe(inOwnWords);
  expect(await line(await askToDelete())).toBe("0px");
});

test("the dialog that shows a link's address has a line around the button that opens it", async ({
  chat,
  page,
}) => {
  // The dialog is the markdown library's own, and so are its classes: the line is
  // drawn by a rule of the app's styles (app/globals.css).
  await chat.open();
  await chat.ask(
    ["this:", "", "Read [the bank's page](https://bank.example/rates)."].join(
      "\n",
    ),
  );
  await chat.answers.getByRole("button", { name: "the bank's page" }).click();
  const dialog = page.locator('[data-streamdown="link-safety-modal"]');
  const open = dialog.getByRole("button", { name: "Open link", exact: true });
  const copy = dialog.getByRole("button", { name: "Copy link", exact: true });
  await expect(open).toBeVisible();

  expect(await line(open)).toBe("1px");
  expect(await line(copy)).toBe("1px");

  // With the page's own colours it is a filled ground again, and has no line; the
  // other button has its own, as ever.
  // The library's button changes over a moment, its line too: waited for.
  await page.emulateMedia({ forcedColors: "none" });
  await expect.poll(() => line(open)).toBe("0px");
  expect(await line(copy)).toBe("1px");
});
