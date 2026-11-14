// Whether the page keeps the newest words in view while a long answer is written, and
// leaves a reader alone who scrolls up. The component tests check the rules with
// made-up sizes (components/chat/follow.test.tsx); only a real browser lays a page out.
import { expect, test } from "./chat";

// A screen small enough that an answer soon runs past its end.
test.use({ viewport: { width: 1000, height: 600 } });

/** An answer of some sixty lines, written over about ten seconds. */
const LONG = `[[pace: 0.015]] ${Array.from({ length: 700 }, (_, index) => `word${String(index)}`).join(" ")}`;

/** Less than two lines of text. The page moves to the end when the browser next draws,
 * so a look that falls between a new word and the drawing sees it a line behind. */
const AT_THE_END = 40;

test("the page stays at the newest words, and leaves a reader who scrolls up where they are", async ({
  chat,
  page,
}) => {
  const newest = page.getByRole("button", { name: "Go to the newest message" });
  await chat.open();
  await chat.ask(LONG);

  // Once the answer no longer fits, some seconds in, the page is at its end, look
  // after look.
  await expect
    .poll(() => chat.hidden(), { timeout: 20_000 })
    .toBeGreaterThan(300);
  for (let look = 0; look < 5; look++) {
    expect(await chat.below()).toBeLessThan(AT_THE_END);
    await page.waitForTimeout(100);
  }
  await expect(newest).toBeHidden();

  // The reader scrolls up a little, as a hand on a trackpad begins.
  await chat.scroller.hover();
  await page.mouse.wheel(0, -6);
  await expect(newest).toBeVisible();
  const where = await chat.scroller.evaluate((element) => element.scrollTop);
  const hidden = await chat.hidden();

  // The answer goes on, and the reader stays.
  await expect
    .poll(() => chat.hidden(), { timeout: 20_000 })
    .toBeGreaterThan(hidden + 150);
  expect(await chat.scroller.evaluate((element) => element.scrollTop)).toBe(
    where,
  );

  // The button leads back to the end, and the page follows again.
  await newest.click();
  await expect(newest).toBeHidden();
  for (let look = 0; look < 5; look++) {
    expect(await chat.below()).toBeLessThan(AT_THE_END);
    await page.waitForTimeout(100);
  }

  // And when the answer has ended, some seconds from now, the page is at its very end.
  await expect(chat.answers).toContainText("word699", { timeout: 20_000 });
  await chat.answered();
  await expect.poll(() => chat.below()).toBeLessThanOrEqual(1);
});
