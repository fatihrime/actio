// An answer is shown as markdown from the moment it is on the page: never first as the
// marks it was written in, to be swapped a moment later (ADR 0016, revision of
// 2026-11-21). What only shows in a real browser, with the code that reads markdown
// fetched over a real network: after a reload, and in the first answer of a visit. And
// when a first visit asks for that code: once the list of conversations has come
// (revision of 2026-11-22).
import type { Page } from "@playwright/test";

import { expect, test } from "./chat";
import { isAppScript, scriptsNamedIn } from "./scripts";

/** A question whose echo is told apart at once: as written, it holds "##" and "**";
 * as shown, a heading and words in bold. No directive in it, so its lines stay. */
const QUESTION = [
  "this:",
  "",
  "## Bike parts",
  "",
  "The **saddle** and the **chain**.",
].join("\n");

/** A question whose answer the model takes a moment to begin, as models do. A
 * directive folds a question's lines into one, so this one's marks are all in a line:
 * as written, its echo holds "**"; as shown, words in bold. */
const SLOW_QUESTION = "[[delay: 1.5]] The **saddle** and the **chain**.";

/** The marks of the questions' markdown. */
const MARKS = /##|\*\*/;

const ANSWER = 'article[aria-label="Actio"]';

/** From before the page's first script runs, and in every page loaded after, note
 * the text of each answer every time the page changes: what was put into an answer
 * and what was taken out of it, and each answer that was put on the page. A text
 * that was shown and swapped at once is noted too, as the text taken out. */
async function watchAnswers(page: Page): Promise<void> {
  await page.addInitScript((answer) => {
    const seen: string[] = [];
    Reflect.set(window, "answersSeen", seen);
    function inAnswer(node: Node): boolean {
      const element = node instanceof Element ? node : node.parentElement;
      return (element?.closest(answer) ?? null) !== null;
    }
    new MutationObserver((records) => {
      for (const record of records) {
        if (inAnswer(record.target)) {
          for (const node of [...record.addedNodes, ...record.removedNodes]) {
            seen.push(node.textContent ?? "");
          }
          if (record.type === "characterData") {
            seen.push(record.oldValue ?? "", record.target.textContent ?? "");
          }
          continue;
        }
        for (const node of record.addedNodes) {
          if (node instanceof Element) {
            for (const added of [node, ...node.querySelectorAll(answer)]) {
              if (added.matches(answer)) {
                seen.push(added.textContent);
              }
            }
          }
        }
      }
    }).observe(document, {
      childList: true,
      subtree: true,
      characterData: true,
      characterDataOldValue: true,
    });
  }, ANSWER);
}

/** What `watchAnswers` noted in the page that is open now. */
async function answersSeen(page: Page): Promise<string[]> {
  return page.evaluate(() => Reflect.get(window, "answersSeen") as string[]);
}

/** Wait until the answer is shown whole, as markdown. */
async function shownWhole(page: Page): Promise<void> {
  const answer = page.locator(ANSWER);
  await expect(
    answer.getByRole("heading", { level: 2, name: "Bike parts" }),
  ).toBeVisible();
  await expect(answer).toContainText("The saddle and the chain.");
}

test("a stored answer is never shown as the marks it was written in, after a reload", async ({
  chat,
  page,
}) => {
  await chat.open();
  await chat.ask(QUESTION);
  await shownWhole(page);
  await chat.answered();

  await watchAnswers(page);
  await page.reload();

  await shownWhole(page);
  const seen = await answersSeen(page);
  expect(seen.some((text) => text.includes("Bike parts"))).toBe(true);
  expect(seen.filter((text) => MARKS.test(text))).toEqual([]);
});

test("the first answer of a visit is never shown as the marks it was written in", async ({
  chat,
  page,
}) => {
  await watchAnswers(page);
  await chat.open();
  await expect(chat.box).toBeVisible();

  // Asked at once. The page began to fetch the code that reads markdown once the list
  // of conversations had come, and the model takes a second and a half to begin:
  // the code is here before the answer's first word. Were it fetched only when that
  // word is shown, as it used to be, the word would be shown as written first.
  await chat.ask(SLOW_QUESTION);

  const answer = page.locator(ANSWER);
  await expect(answer).toHaveText("Echo: The saddle and the chain.");
  await expect(answer.locator('[data-streamdown="strong"]')).toHaveText([
    "saddle",
    "chain",
  ]);
  await chat.answered();
  const seen = await answersSeen(page);
  expect(seen.some((text) => text.includes("saddle"))).toBe(true);
  expect(seen.filter((text) => MARKS.test(text))).toEqual([]);
});

test("a first visit asks for the code that draws markdown once the list of conversations has come, and not before", async ({
  chat,
  page,
}) => {
  // Each of the app's scripts that the page asks for, by its path, and whether the
  // list was still held back then.
  const asked: { path: string; listHeld: boolean }[] = [];
  let listHeld = true;
  page.on("request", (request) => {
    if (isAppScript(request)) {
      asked.push({ path: new URL(request.url()).pathname, listHeld });
    }
  });
  let release: () => void = () => undefined;
  const released = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/conversations*", async (route) => {
    await released;
    await route.continue();
  });

  const response = await page.goto("/");
  // What the page needs to start is named in it. The code that draws markdown is a
  // piece apart, which it asks for later.
  const named = new Set(scriptsNamedIn((await response?.text()) ?? ""));
  expect(named.size).toBeGreaterThan(1);
  const apart = (whileListHeld: boolean) =>
    asked
      .filter(
        (script) =>
          script.listHeld === whileListHeld && !named.has(script.path),
      )
      .map((script) => script.path);

  await expect(chat.box).toBeVisible();
  // Time enough for a page that asked as soon as it knew whose session it has, as it
  // did before, to have asked.
  await page.waitForTimeout(500);
  expect(apart(true)).toEqual([]);

  listHeld = false;
  release();

  await expect(chat.list.getByText(/No conversations yet/)).toBeVisible();
  await expect.poll(() => apart(false).length).toBeGreaterThan(0);
});
