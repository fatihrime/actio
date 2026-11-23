// Under an answer that has ended, one quiet line says what it took (ADR 0015, revision
// "an answer says what it took"). What only the whole stack shows: the server's own
// clock and counts behind the line, a page that keeps it in view, and the same words
// once the conversation is read again. The rules and their words are the component
// tests' (lib/chat/figures.test.ts, components/chat/figures.test.tsx).
import { expect, test } from "./chat";

// A screen small enough that the conversation runs past its end.
test.use({ viewport: { width: 1000, height: 600 } });

/** Thirty short paragraphs, which the fake model echoes a word at a time, over about a
 * second: its server says nothing of how long it took to write them, so the page counts
 * the rate by the server's clock. Short, since the first question's directives hold
 * the worker's title job as long. */
const QUESTION = `[[pace: 0.035]] ${Array.from({ length: 30 }, (_, index) => `line${String(index)}`).join("\n\n")}`;

test("an answer that has ended says how fast, how soon and what it cost, in view, and the same once read again", async ({
  chat,
  page,
}) => {
  await chat.open();
  await chat.ask(QUESTION);
  const took = page.getByRole("list", { name: "What this answer took" });
  const figures = took.getByRole("listitem");

  await expect(chat.answers).toContainText("line29");
  await chat.answered();

  await expect(figures).toHaveText([
    /^(\d\.\d|\d[\d,]*) tokens a second$/,
    /^first word after (\d+ ms|\d\.\d s|\d+ s)$/,
    /^\d[\d,]* credits?$/,
  ]);
  // The conversation is longer than the screen, and the page followed its end.
  expect(await chat.hidden()).toBeGreaterThan(0);
  await expect(took).toBeInViewport({ ratio: 1 });
  const before = await figures.allTextContents();

  await page.reload();

  await expect(chat.answers).toContainText("line29");
  await expect(figures).toHaveText(before);
});
