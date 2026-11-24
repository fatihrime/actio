// Code is coloured in a worker, off the page's thread (ADR 0016, revision "code is
// coloured in a worker"): on the page's own thread, a first block of TypeScript kept
// the page still for as long as 1.5 s. What only a real browser shows: that the worker
// starts with the first block of code, that there is one, and that what it answers is
// drawn. How long anything takes is not asserted: no test may depend on how fast a
// machine is.
//
// And a block of code has its real height from the first layout, also far from the
// window (app/globals.css): the markdown library would draw it as a smaller
// placeholder until it had been near the window. So a conversation left with a line of
// code at the top of the window opens again at that line.
//
// The fake model echoes the question, so a question that holds blocks of code after an
// empty line is answered with those blocks.
import type { Locator, Page } from "@playwright/test";

import { expect, test } from "./chat";
import type { Chat } from "./chat";

/** A block of code, with its language's name above it. */
const CODE_BLOCK = '[data-streamdown="code-block"]';

/** A question whose echo holds these blocks of code, each in its language. */
function withCode(...blocks: [language: string, code: string][]): string {
  return [
    "this:",
    ...blocks.flatMap(([language, code]) => [
      "",
      `\`\`\`${language}`,
      code,
      "```",
    ]),
  ].join("\n");
}

/** Wait until a block of code (`CODE_BLOCK`) is shown in colours. A token with colours
 * carries the dark theme's too; code that has none yet is plain, in the colour around
 * it. */
async function coloured(block: Locator): Promise<void> {
  await expect(
    block.locator('pre span[style*="--shiki-dark"]'),
  ).not.toHaveCount(0);
}

test("code is coloured by a worker, which starts with the first block of code", async ({
  chat,
  page,
}) => {
  await chat.open();
  await chat.ask("Which saddle fits a road bike?");
  await expect(chat.answers).toHaveText("Echo: Which saddle fits a road bike?");
  await chat.answered();
  // A page that has shown no code has started no worker.
  expect(page.workers()).toEqual([]);

  await chat.ask(
    withCode(["ts", "const wheels: number = 2; // a road bike's, at least"]),
  );
  const typescript = chat.answers.nth(1).locator(CODE_BLOCK);
  await expect(typescript.locator("pre")).toContainText(
    "const wheels: number = 2;",
  );
  // A keyword, in the light theme's colour for it.
  await expect(
    typescript.locator("pre span", { hasText: /^const$/ }),
  ).toHaveAttribute("style", /--sdm-c: ?#[0-9a-f]{6}/i);
  await coloured(typescript);
  await expect.poll(() => page.workers().length).toBe(1);
  await chat.answered();

  await chat.ask(withCode(["css", ".saddle { margin: 0 auto; color: #333; }"]));
  const css = chat.answers.nth(2).locator(CODE_BLOCK);
  await expect(css.locator("pre")).toContainText(".saddle");
  await coloured(css);
  expect(page.workers()).toHaveLength(1);
});

test("a stored conversation with code in several languages is coloured after a reload", async ({
  chat,
  page,
}) => {
  // The first three are the languages that kept the page still the longest.
  await chat.open();
  await chat.ask(
    withCode(
      [
        "ts",
        [
          "/* Velora Cycles: the parts of a bike. */",
          'const parts: string[] = ["saddle", "chain"];',
        ].join("\n"),
      ],
      ["css", ".saddle {\n  margin: 0 auto;\n}"],
      [
        "cpp",
        "#include <vector>\n\nint main() {\n  std::vector<int> spokes(32);\n  return 0;\n}",
      ],
      ["python", "def total(prices):\n    return sum(prices)"],
    ),
  );
  const blocks = chat.answers.locator(CODE_BLOCK);
  await expect(blocks).toHaveCount(4);
  await chat.answered();

  await page.reload();

  await expect(blocks).toHaveCount(4);
  for (const language of ["ts", "css", "cpp", "python"]) {
    await coloured(
      chat.answers.locator(`${CODE_BLOCK}[data-language="${language}"]`),
    );
  }
  // The page before the reload took its worker with it.
  await expect.poll(() => page.workers().length).toBe(1);
});

/** A block of code of `count` lines, each its own: `const spoke1 = 1;` and on. */
function spokes(count: number): string {
  return Array.from(
    { length: count },
    (_, index) => `const spoke${String(index + 1)} = ${String(index + 1)};`,
  ).join("\n");
}

/** A question of `count` lines of plain words, whose echo is as long: what puts the
 * conversation's earlier messages far above the window. */
function aLongQuestion(count: number): string {
  return Array.from(
    { length: count },
    (_, index) => `Rim ${String(index + 1)}`,
  ).join("\n");
}

/** Ask for an answer with a block of code of `lines` lines, and then a long question
 * below it, and wait for both answers. */
async function codeAndThenMore(chat: Chat, lines: number): Promise<void> {
  await chat.open();
  await chat.ask(withCode(["ts", spokes(lines)]));
  await expect(chat.answers.locator(CODE_BLOCK)).toHaveCount(1);
  await chat.answered();
  await chat.ask(aLongQuestion(100));
  await expect(chat.answers).toHaveCount(2);
  await chat.answered();
}

/** Wait for `frames` frames of the page. */
async function frames(page: Page, frames: number): Promise<void> {
  await page.evaluate(
    (left) =>
      new Promise<void>((resolve) => {
        const next = (count: number) => {
          requestAnimationFrame(() => {
            if (count > 1) {
              next(count - 1);
            } else {
              resolve();
            }
          });
        };
        next(left);
      }),
    frames,
  );
}

test("a block of code far above the window is as high before it has been in view as after", async ({
  chat,
  page,
}) => {
  await codeAndThenMore(chat, 40);
  // Its elements drawn anew, with the conversation at its end, far below the block: a
  // browser remembers a block's height only for as long as its element lives.
  await page.reload();
  await expect(chat.answers).toHaveCount(2);
  await frames(page, 3);
  const block = chat.answers.first().locator(CODE_BLOCK);

  const before = await block.evaluate((element) => {
    const scroller = element.closest('[role="log"]')?.parentElement;
    const box = element.getBoundingClientRect();
    return {
      height: box.height,
      // How far above the window's top its bottom is, in windows.
      above:
        scroller === null || scroller === undefined
          ? 0
          : (scroller.getBoundingClientRect().top - box.bottom) /
            scroller.clientHeight,
    };
  });
  await block.evaluate((element) => {
    element.scrollIntoView();
  });
  await frames(page, 3);
  const after = await block.evaluate(
    (element) => element.getBoundingClientRect().height,
  );

  // The controls: it was more than a window above, and its 40 lines are far taller
  // than the library's placeholder of 200 pixels and its frame.
  expect(before.above).toBeGreaterThan(1);
  expect(after).toBeGreaterThan(500);
  expect(Math.abs(before.height - after)).toBeLessThanOrEqual(1);
});

/** In the page: from now on, at the start of each frame, note how far below the top of
 * the line of code that holds `text` the top of the part that scrolls is, in pixels,
 * from the first frame that shows that line. */
function watchLine(text: string): void {
  const offsets: number[] = [];
  Reflect.set(window, "lineOffsets", offsets);
  function look() {
    const scroller = document.querySelector('[role="log"]')?.parentElement;
    const line = [
      ...document.querySelectorAll('[role="log"] article pre code > span'),
    ].find((each) => each.textContent.includes(text));
    if (scroller !== null && scroller !== undefined && line !== undefined) {
      offsets.push(
        scroller.getBoundingClientRect().top - line.getBoundingClientRect().top,
      );
    }
    requestAnimationFrame(look);
  }
  requestAnimationFrame(look);
}

test("a conversation left with a line of code at the top of the window opens again at that line, from its first frame", async ({
  chat,
  page,
}) => {
  await codeAndThenMore(chat, 60);
  // The reader goes up to the 30th line of the answer's code, a few pixels into it.
  const left = await chat.answers
    .first()
    .locator(CODE_BLOCK)
    .evaluate((block) => {
      const scroller = block.closest('[role="log"]')?.parentElement;
      const line = [...block.querySelectorAll("pre code > span")].find((each) =>
        each.textContent.includes("spoke30 "),
      );
      if (scroller === null || scroller === undefined || line === undefined) {
        throw new Error("The line to go to isn't on the page.");
      }
      const top = () => scroller.getBoundingClientRect().top;
      scroller.scrollTop += line.getBoundingClientRect().top - top() + 6;
      return top() - line.getBoundingClientRect().top;
    });
  await expect(
    page.getByRole("button", { name: "Go to the newest message" }),
  ).toBeVisible();
  await chat.newChat.click();
  await expect(chat.questions).toHaveCount(0);
  await page.evaluate(watchLine, "spoke30 ");

  await chat.listed.first().click();

  await expect(chat.answers).toHaveCount(2);
  await expect
    .poll(() =>
      page.evaluate(
        () => (Reflect.get(window, "lineOffsets") as number[]).length,
      ),
    )
    .toBeGreaterThan(20);
  const offsets = await page.evaluate(
    () => Reflect.get(window, "lineOffsets") as number[],
  );
  // The control: the reader was a few pixels into that line.
  expect(Math.abs(left - 6)).toBeLessThanOrEqual(1);
  expect(offsets.filter((offset) => Math.abs(offset - left) > 2)).toEqual([]);
});
