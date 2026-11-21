// An answer whose markdown can't be drawn is shown as it was written, and the rest of
// the page goes on (ADR 0016, revision "an answer that can't be drawn is shown as
// written"). What only a real browser shows: a part of the library that can't be
// fetched, as when the installation was upgraded under a page that stayed open. The
// library fetches the part that draws a block of code with the first block, and
// React keeps a part that failed failed.
//
// The fake model echoes the question, so a question that holds a block of code after
// an empty line is answered with that block.
import { expect, test } from "./chat";

/** The page's scripts, from the build: Next.js serves them under `/_next/static/`. */
function isScript(url: URL): boolean {
  return (
    url.pathname.startsWith("/_next/static/") && url.pathname.endsWith(".js")
  );
}

test("an answer whose code can't be drawn is shown as written, and the page goes on", async ({
  chat,
  page,
}) => {
  await chat.open();
  await chat.ask("The **saddle** first.");
  // The code that draws markdown is here.
  await expect(
    chat.answers.first().locator('[data-streamdown="strong"]'),
  ).toHaveText("saddle");
  await chat.answered();

  // Every script the page asks for from now on is refused: the name of the file that
  // draws a block of code is the build's, and can't be known here. Turbopack's loader
  // asks for a file twice before it gives up, so it is refused each time. The
  // browser's console says each refusal, and React the error it caught: neither is an
  // error that the page's scripts left uncaught, which would fail the test.
  await page.route(isScript, (route) => route.abort());
  const question = ["this:", "", "```python", "print('saddle')", "```"].join(
    "\n",
  );
  await chat.ask(question);

  // The answer as it was written, its fence marks included, and no block of code.
  const answer = chat.answers.nth(1);
  await expect(answer).toContainText("```python");
  await chat.answered();
  await expect(answer).toHaveText(`Echo: ${question}`);
  await expect(answer.locator("pre")).toHaveCount(0);
  // The rest of the page is as it was.
  await expect(
    chat.answers.first().locator('[data-streamdown="strong"]'),
  ).toHaveText("saddle");
  await expect(chat.box).toBeVisible();

  // With the scripts let through again, a next question is asked and answered.
  await page.unroute(isScript);
  await chat.ask("Then the **chain**.");

  await expect(
    chat.answers.nth(2).locator('[data-streamdown="strong"]'),
  ).toHaveText("chain");
  await expect(chat.answers.nth(2)).toHaveText("Echo: Then the chain.");
  await chat.answered();
});
