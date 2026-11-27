// The reader chooses the model that answers, in a menu that the box's row opens (ADR
// 0032). What only the whole stack and a real browser show: the request the page sends,
// naming the model chosen, answered by that model at its own price; the choice kept by
// the browser over a reload and in a new chat; the picker driven by real keys, where a
// browser puts the keyboard on nothing inside an element that is hidden, and goes on
// from a row that Tab hid; the box's row in a window 320 px wide, where styles decide
// what the switch for reasoning shows and how much of a long title; and a model whose
// server isn't there, chosen, answered for by the one that stands in for it. The stack
// offers seven models, one of them under a title of 21 characters
// (deploy/e2e/config/models.toml). The rule's variants are the component tests'
// (components/chat/model-picker.test.tsx, lib/chat/chosen.test.ts).
import type { Locator, Page, Request } from "@playwright/test";

import { expect, test } from "./chat";

/** The page's next request for an answer. */
function nextTurn(page: Page): Promise<Request> {
  return page.waitForRequest(
    (request) =>
      request.method() === "POST" &&
      /^\/api\/conversations\/[^/]+\/turns$/.test(
        new URL(request.url()).pathname,
      ),
  );
}

/** What a request for an answer named, as far as this test reads it. */
function asked(request: Request): { model?: string } {
  return request.postDataJSON() as { model?: string };
}

function pickerOf(page: Page) {
  const menu = page.getByRole("menu", { name: "Model" });
  return {
    button: page.getByRole("button", { name: /^Model: / }),
    menu,
    row: (name: string) => menu.getByRole("menuitemradio", { name }),
  };
}

/** The line under the last answer that says what it took. */
function figuresOf(page: Page) {
  return page
    .getByRole("list", { name: "What this answer took" })
    .last()
    .getByRole("listitem");
}

test("a model chosen with the mouse answers at its own price, and stays chosen after a reload and in a new chat", async ({
  chat,
  page,
}) => {
  const picker = pickerOf(page);
  await chat.open();
  await expect(picker.button).toHaveAccessibleName("Model: Fake model");

  await picker.button.click();
  await picker.row("Local").click();

  await expect(picker.menu).toBeHidden();
  await expect(picker.button).toHaveAccessibleName("Model: Local");
  const turn = nextTurn(page);
  await chat.ask("Name a bike part.");
  expect(asked(await turn).model).toBe("local");
  await expect(chat.answers).toHaveText("Echo: Name a bike part.");
  await chat.answered();
  // Local costs nothing on this stack (deploy/e2e/config/plans.toml).
  await expect(figuresOf(page).filter({ hasText: /credits?$/ })).toHaveText(
    "0 credits",
  );

  await page.reload();

  await expect(chat.answers).toHaveText("Echo: Name a bike part.");
  await expect(picker.button).toHaveAccessibleName("Model: Local");

  await chat.newChat.click();

  await expect(chat.questions).toHaveCount(0);
  await expect(picker.button).toHaveAccessibleName("Model: Local");
});

test("the picker is used with the keys alone, and the keyboard is put on a row once the menu is shown", async ({
  chat,
  page,
}) => {
  const picker = pickerOf(page);
  await chat.open();
  await expect(picker.button).toHaveAccessibleName("Model: Fake model");
  // A question is in the box, ready to send: a key that chose and sent would show.
  await chat.box.fill("Not sent yet.");

  // The next stop after the box.
  await page.keyboard.press("Tab");
  await expect(picker.button).toBeFocused();
  await page.keyboard.press("Enter");

  await expect(picker.menu).toBeVisible();
  await expect(picker.button).toHaveAttribute("aria-expanded", "true");
  await expect(picker.row("Fake model")).toHaveAttribute(
    "aria-checked",
    "true",
  );
  await expect(picker.row("Fake model")).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(picker.row("Local")).toBeFocused();
  await page.keyboard.press("Enter");

  await expect(picker.menu).toBeHidden();
  await expect(picker.button).toBeFocused();
  await expect(picker.button).toHaveAccessibleName("Model: Local");
  // Enter chose, and sent nothing: the picker stands in the box's form.
  await expect(chat.questions).toHaveCount(0);
  await expect(chat.box).toHaveValue("Not sent yet.");

  await page.keyboard.press("ArrowDown");
  await expect(picker.row("Local")).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(picker.row("Namer")).toBeFocused();
  await page.keyboard.press("Escape");

  await expect(picker.menu).toBeHidden();
  await expect(picker.button).toBeFocused();
  await expect(picker.button).toHaveAccessibleName("Model: Local");

  // Tab closes the menu, and the keyboard goes on from where its row was, a row that
  // is hidden by then: to what follows the picker, which for a model that can't reason
  // is Send, and with Shift back to the picker's button.
  await page.keyboard.press("Enter");
  await expect(picker.row("Local")).toBeFocused();
  await page.keyboard.press("Tab");

  await expect(picker.menu).toBeHidden();
  await expect(chat.send).toBeFocused();

  await page.keyboard.press("Shift+Tab");
  await expect(picker.button).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(picker.row("Local")).toBeFocused();
  await page.keyboard.press("Shift+Tab");

  await expect(picker.menu).toBeHidden();
  await expect(picker.button).toBeFocused();
  await expect(chat.questions).toHaveCount(0);
});

test("in a window 320 px wide the box's row stays one line, and the switch for reasoning keeps its name", async ({
  chat,
  page,
}) => {
  await page.setViewportSize({ width: 320, height: 640 });
  const picker = pickerOf(page);
  await chat.open();
  // The fake under the default name can reason: its switch stands beside the picker.
  await expect(picker.button).toHaveAccessibleName("Model: Fake model");
  const reasoning = page.getByRole("checkbox", {
    name: "Think before answering",
    exact: true,
  });

  // It shows one word there, and is named by all of them.
  const label = page.locator("label").filter({ has: reasoning });
  await expect(reasoning).toBeVisible();
  await expect(label).toHaveText("Think", { useInnerText: true });

  const place = async (of: Locator) => {
    const box = await of.boundingBox();
    if (box === null) {
      throw new Error("Something of the box's row isn't drawn.");
    }
    return { ...box, middle: box.y + box.height / 2, right: box.x + box.width };
  };
  const button = await place(picker.button);
  const toggle = await place(label);
  const send = await place(chat.send);
  const form = await place(page.locator("form"));
  // One line: the three at one height, in their order, inside the box.
  expect(Math.abs(button.middle - send.middle)).toBeLessThan(4);
  expect(Math.abs(toggle.middle - send.middle)).toBeLessThan(4);
  expect(button.x).toBeGreaterThanOrEqual(form.x);
  expect(button.right).toBeLessThanOrEqual(toggle.x);
  expect(toggle.right).toBeLessThanOrEqual(send.x);
  expect(send.right).toBeLessThanOrEqual(form.right);
  // And the page is no wider than the window.
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(320);

  // The menu opens inside the window.
  await picker.button.click();
  const menu = await place(picker.menu);
  expect(menu.x).toBeGreaterThanOrEqual(0);
  expect(menu.y).toBeGreaterThanOrEqual(0);
  expect(menu.right).toBeLessThanOrEqual(320);

  // A model that can reason too, with a title of 21 characters, which the row has no
  // room for beside the switch: the title gives way, and ends in "…".
  await picker.row("Hurried, in 3 seconds").click();
  await expect(picker.button).toHaveAccessibleName(
    "Model: Hurried, in 3 seconds",
  );
  await expect(label).toHaveText("Think", { useInnerText: true });
  const title = picker.button.getByText("Hurried, in 3 seconds");
  expect(
    await title.evaluate((shown) => shown.scrollWidth > shown.clientWidth),
  ).toBe(true);
  const long = await place(picker.button);
  const beside = await place(label);
  expect(Math.abs(long.middle - send.middle)).toBeLessThan(4);
  expect(Math.abs(beside.middle - send.middle)).toBeLessThan(4);
  expect(long.x).toBeGreaterThanOrEqual(form.x);
  expect(long.right).toBeLessThanOrEqual(beside.x);
  expect(beside.right).toBeLessThanOrEqual(send.x);
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(320);
});

test("a model whose server isn't there, chosen, is answered for by the one that stands in for it, and the answer says so", async ({
  chat,
  page,
}) => {
  const picker = pickerOf(page);
  await chat.open();
  await picker.button.click();
  await picker.row("Away").click();
  await expect(picker.button).toHaveAccessibleName("Model: Away");

  const turn = nextTurn(page);
  await chat.ask("Name a bike part.");

  expect(asked(await turn).model).toBe("away");
  await expect(chat.answers).toHaveText("Echo: Name a bike part.");
  await chat.answered();
  await expect(figuresOf(page).last()).toHaveText("answered by Fake model");
});
