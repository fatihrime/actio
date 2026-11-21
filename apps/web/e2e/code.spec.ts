// Code is coloured in a worker, off the page's thread (ADR 0016, revision "code is
// coloured in a worker"): on the page's own thread, a first block of TypeScript kept
// the page still for as long as 1.5 s. What only a real browser shows: that the worker
// starts with the first block of code, that there is one, and that what it answers is
// drawn. How long anything takes is not asserted: no test may depend on how fast a
// machine is.
//
// The fake model echoes the question, so a question that holds blocks of code after an
// empty line is answered with those blocks.
import type { Locator } from "@playwright/test";

import { expect, test } from "./chat";

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
