// An arriving answer is drawn at most every 50 ms (ADR 0016, revision "an arriving
// answer is drawn at most every 50 ms"). What only a real browser shows: how often the
// page changes while an answer arrives faster than it draws frames. The rule, the clock
// it is read by, and what is drawn when, are the component tests'
// (components/chat/paced.test.tsx).
//
// The page is watched from inside, at each frame, by a loop that begins before the
// question is sent. What the driver sees between two of its own calls is no frame. No
// test says how long a frame took: on a machine shared with other work that says
// little.
import { expect, test } from "./chat";

/** How long the page leaves an arriving answer as it is at the least, in
 * milliseconds: `DRAWN_EVERY_MS` in components/chat/paced.ts. */
const DRAWN_EVERY_MS = 50;

/** 300 words, which the fake model echoes after "Echo: ", at 200 words a second. It
 * leaves the directive out. */
const WORDS = Array.from(
  { length: 300 },
  (_, index) => `word${String(index)}`,
).join(" ");
const QUESTION = `[[pace: 0.005]] ${WORDS}`;

/** A frame at which the answer's text was not what it was at the one before. */
interface Change {
  /** When, in milliseconds since the page started. */
  at: number;
  /** How long the text was, in characters. */
  length: number;
}

/** In the page: from now on, note at the start of each frame how long the newest
 * answer's text is, whenever it changed. */
function watchAnswer(): void {
  const changes: Change[] = [];
  Reflect.set(window, "answerChanges", changes);
  let last = 0;
  function look() {
    const answers = document.querySelectorAll('article[aria-label="Actio"]');
    const text = answers[answers.length - 1]?.textContent ?? "";
    // Until its words begin, the answer says that it waits for the model.
    const length = text.startsWith("Echo:") ? text.length : 0;
    if (length !== last) {
      changes.push({ at: performance.now(), length });
      last = length;
    }
    requestAnimationFrame(look);
  }
  requestAnimationFrame(look);
}

test("an arriving answer is drawn at most every 50 ms, and whole at its end", async ({
  chat,
  page,
}) => {
  await chat.open();
  await expect(chat.box).toBeVisible(); // the page has its session
  await page.evaluate(watchAnswer);

  await chat.ask(QUESTION);

  await expect(chat.answers).toHaveText(`Echo: ${WORDS}`);
  await chat.answered();
  const changes = await page.evaluate(
    () => Reflect.get(window, "answerChanges") as Change[],
  );
  // The control: the answer came onto the page in pieces, and not only at its end.
  expect(changes.length).toBeGreaterThanOrEqual(5);
  const elapsed = (changes.at(-1)?.at ?? 0) - (changes[0]?.at ?? 0);
  // Each drawing is at least 50 ms after the one before it was on the page, the frame
  // that shows one can be a frame late, and the whole answer at its end is one more.
  expect(changes.length).toBeLessThanOrEqual(
    Math.ceil(elapsed / DRAWN_EVERY_MS) + 3,
  );
});
