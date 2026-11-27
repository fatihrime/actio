// The reader chooses the model that answers, in a menu that the box's row opens (ADR
// 0032). What only the whole stack and a real browser show: the request the page sends,
// naming the model chosen, answered by that model at its own price; the choice kept by
// the browser over a reload and in a new chat; the picker driven by real keys, where a
// browser puts the keyboard on nothing inside an element that is hidden, and goes on
// from a row that Tab hid; the box's row in a window 320 px wide, where styles decide
// what the switch for reasoning shows and how much of a long title; a model whose
// server isn't there, chosen, answered for by the one that stands in for it; what
// each model costs, from the stack's plans file through the API, a line under its
// title; and what a question to a model would meet now, by the API's look at the
// stack's Valkey and its real chains, a line before the cost: which model answers for
// one whose server isn't there, and that one is at its provider's limit. The stack
// offers seven models, one of them under a title of 21 characters
// (deploy/e2e/config/models.toml), at three kinds of price (plans.toml beside it). The
// rule's variants are the component tests' (components/chat/model-picker.test.tsx,
// lib/chat/chosen.test.ts, lib/chat/prices.test.ts, lib/chat/outcomes.test.ts).
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
  await expect(figuresOf(page).last()).toHaveText(
    "answered by Fake model in place of Away",
  );
});

test("the menu says under each model's title what it costs, and which model answers for one that can't, and a row is named by its title alone", async ({
  chat,
  page,
}) => {
  const picker = pickerOf(page);
  await chat.open();

  await picker.button.click();

  await expect(picker.menu).toBeVisible();
  // The stack's prices (deploy/e2e/config/plans.toml): Local costs nothing, Namer has
  // the shipped price, and the others a credit a word, read or written. Away's server
  // isn't there, and the model that stands in for it is ready: the list says Away is
  // ready, and that a question to it is given to the default model first, which its
  // row says before its cost. The default model's row says nothing before its cost:
  // its breaker, which Local shares, opens on three failed calls of five at the least,
  // and one run of these tests has the fake fail two (e2e/turns.spec.ts).
  const lines = {
    "Fake model": ["1 credit a token"],
    Local: ["Free"],
    Namer: ["1 credit a token written, 0.25 read"],
    Away: ["Fake model answers for now", "1 credit a token"],
  };
  for (const [title, said] of Object.entries(lines)) {
    const row = picker.row(title);
    await expect(row).toHaveAccessibleName(title);
    await expect(row).toHaveAccessibleDescription(said.join(" "));
    for (const line of said) {
      await expect(row.getByText(line, { exact: true })).toBeVisible();
    }
  }
  // A line under the title, inside the menu.
  const namer = picker.row("Namer");
  const title = await namer.getByText("Namer", { exact: true }).boundingBox();
  const cost = await namer
    .getByText("1 credit a token written, 0.25 read", { exact: true })
    .boundingBox();
  const menu = await picker.menu.boundingBox();
  if (title === null || cost === null || menu === null) {
    throw new Error("Something of the menu isn't drawn.");
  }
  expect(cost.y).toBeGreaterThanOrEqual(title.y + title.height - 1);
  expect(Math.abs(cost.x - title.x)).toBeLessThan(1);
  expect(cost.x + cost.width).toBeLessThanOrEqual(menu.x + menu.width);
  // And as they are drawn, a row's two lines are in the order of its description: who
  // answers above what it costs.
  const away = picker.row("Away");
  const answers = await away
    .getByText("Fake model answers for now", { exact: true })
    .boundingBox();
  const awayCost = await away
    .getByText("1 credit a token", { exact: true })
    .boundingBox();
  if (answers === null || awayCost === null) {
    throw new Error("Something of Away's row isn't drawn.");
  }
  expect(awayCost.y).toBeGreaterThanOrEqual(answers.y + answers.height - 1);
});

test("a model chosen and asked until it is at its provider's limit says so in its row, before its cost", async ({
  chat,
  page,
}) => {
  const picker = pickerOf(page);
  /** What Rationed's row says while a question to it is refused for now: for a minute
   * at the most, which is said "about 1 minute", and in its last second "under a
   * minute". */
  const atItsLimit =
    /^At its provider's limit for (under a minute|about 1 minute) 1 credit a token$/;
  await chat.open();
  await picker.button.click();
  await picker.row("Rationed").click();
  await expect(picker.button).toHaveAccessibleName("Model: Rationed");

  // Rationed has a budget of one request a minute (deploy/e2e/config/models.toml),
  // counted in the stack's Valkey, which keeps the count between tests: a fresh stack
  // has the request, and a stack kept running, or e2e/no-budget.spec.ts beside this
  // test, may have taken it. So the test asks until the list, read again as the menu
  // opens, says that Rationed is at its limit:
  //
  // - after a question that was answered, which took the minute's request, it says so
  //   for a minute;
  // - after one refused for now it says so too, unless the wait was over by the time
  //   the list was read: then the next question is asked.
  //
  // So two questions at the most: after a refusal whose wait was over, the next is
  // answered, or refused for a minute where the test beside this one took the request
  // in between, and the list says so either way. The loop allows four.
  const questions = [
    "Name a bike part.",
    "Name another.",
    "And a third.",
    "A fourth.",
  ];
  for (const question of questions) {
    const turn = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        /^\/api\/conversations\/[^/]+\/turns$/.test(
          new URL(response.url()).pathname,
        ),
    );
    await chat.ask(question);
    const came = await turn;
    expect(asked(came.request()).model).toBe("rationed");
    if (came.status() === 200) {
      await expect(chat.answers.last()).toHaveText(`Echo: ${question}`);
      await chat.answered();
    } else {
      expect(came.status()).toBe(429);
    }

    const read = page.waitForResponse(
      (response) =>
        response.request().method() === "GET" &&
        new URL(response.url()).pathname === "/api/models",
    );
    await picker.button.click();
    const listed = (await (await read).json()) as {
      models: { id: string; retry_after: number | null }[];
    };
    const rationed = listed.models.find((model) => model.id === "rationed");
    if (rationed !== undefined && rationed.retry_after !== null) {
      const row = picker.row("Rationed");
      await expect(row).toHaveAccessibleName("Rationed");
      await expect(row).toHaveAccessibleDescription(atItsLimit);
      await expect(
        row.getByText(/^At its provider's limit for /),
      ).toBeVisible();
      return;
    }
    await page.keyboard.press("Escape");
    await expect(picker.menu).toBeHidden();
  }
  throw new Error(
    "the list never said that Rationed is at its provider's limit",
  );
});

test("two answers of one conversation, by the default model and by one chosen after it, each say which model wrote it, also after a reload", async ({
  chat,
  page,
}) => {
  const picker = pickerOf(page);
  const lines = page.getByRole("list", { name: "What this answer took" });
  /** What each item of the line under the answer `index` says, from the top. */
  const itemsOf = (index: number) => lines.nth(index).getByRole("listitem");
  await chat.open();
  await expect(picker.button).toHaveAccessibleName("Model: Fake model");

  // Nobody chose: the question names no model, and the default answers.
  const first = nextTurn(page);
  await chat.ask("Name a bike part.");
  expect(asked(await first).model).toBeUndefined();
  await expect(chat.answers).toHaveText("Echo: Name a bike part.");
  await chat.answered();
  await picker.button.click();
  await picker.row("Local").click();
  await expect(picker.button).toHaveAccessibleName("Model: Local");
  const second = nextTurn(page);
  await chat.ask("Name another.");
  expect(asked(await second).model).toBe("local");
  await expect(chat.answers).toHaveText([
    "Echo: Name a bike part.",
    "Echo: Name another.",
  ]);
  await chat.answered();

  await expect(lines).toHaveCount(2);
  await expect(itemsOf(0).last()).toHaveText("answered by Fake model");
  await expect(itemsOf(1).last()).toHaveText("answered by Local");
  const said = [
    await itemsOf(0).allTextContents(),
    await itemsOf(1).allTextContents(),
  ];

  await page.reload();

  await expect(chat.answers).toHaveText([
    "Echo: Name a bike part.",
    "Echo: Name another.",
  ]);
  await expect(lines).toHaveCount(2);
  await expect(itemsOf(0)).toHaveText(said[0] ?? []);
  await expect(itemsOf(1)).toHaveText(said[1] ?? []);
});
