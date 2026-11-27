// A question that no model has budget left for is refused for now (ADR 0030, revision
// of 2026-11-26), and the page counts the wait down above the box until it is over (ADR
// 0021, revision "a refusal for now is counted down"). What only the whole stack shows:
// a real count in the stack's Valkey, the API's real 429 through the proxy, and a real
// browser that counts its wait down. The variants are the component tests'
// (components/chat/refused-for-now.test.tsx, lib/chat/outcomes.test.ts).
import type { Response } from "@playwright/test";

import { expect, test } from "./chat";

/** The stack's model with a budget of one request a minute
 * (deploy/e2e/config/models.toml). */
const RATIONED = "rationed";

/** A wait with this much of it left is long enough to look at: the notice is still
 * there when the test has read the time left twice and presses "Dismiss". */
const LONG_ENOUGH_S = 10;

/** The questions, as far as they are needed. */
const QUESTIONS = [
  "Name a bike part.",
  "Name another bike part.",
  "And a third one.",
  "And a fourth.",
];

/** Whether `response` is the API's answer to a request for a turn. */
function isTurn(response: Response): boolean {
  return (
    response.request().method() === "POST" &&
    /^\/api\/conversations\/[^/]+\/turns$/.test(
      new URL(response.url()).pathname,
    )
  );
}

test("a question no model has budget left for is refused for now, and the page counts the wait down", async ({
  chat,
  page,
}) => {
  // This test was written before the page could choose a model (e2e/picker.spec.ts):
  // every request for a turn it makes is given the name of the one with a budget on
  // its way out. Nothing else of a request is changed.
  await page.route("**/api/conversations/*/turns", async (route) => {
    const request = route.request();
    if (request.method() !== "POST") {
      await route.continue();
      return;
    }
    const asked = JSON.parse(request.postData() ?? "{}") as Record<
      string,
      unknown
    >;
    await route.continue({
      postData: JSON.stringify({ ...asked, model: RATIONED }),
    });
  });
  const refused = page.getByText("No model can take this message right now.", {
    exact: true,
  });
  const timer = page.getByRole("timer");
  await chat.open();

  // The model's one request a minute is counted in the stack's Valkey, which keeps the
  // count between tests: a fresh stack has the request, and a stack kept running, or a
  // run that repeats this test, finds it as the last run left it, and a test of the
  // picker that asks it beside this one may take it (e2e/picker.spec.ts). So the test
  // asks until a question is refused with enough of its wait left to look at:
  //
  // - a question that is answered has just taken the minute's request, and the next,
  //   asked right after, is refused for about a minute;
  // - a question refused for a few seconds more, the end of a minute that an earlier
  //   run began, is waited out: its notice goes by itself, and the next is asked.
  //
  // That is three questions at the most: refused for a moment, answered, refused.
  let question = "";
  let answered = 0;
  let response: Response | null = null;
  for (const next of QUESTIONS) {
    question = next;
    const turn = page.waitForResponse(isTurn);
    await chat.ask(question);
    const came = await turn;
    if (came.status() === 200) {
      answered += 1;
      await expect(chat.answers).toHaveCount(answered);
      await expect(chat.answers.last()).toHaveText(`Echo: ${question}`);
      await chat.answered();
      continue;
    }
    expect(came.status()).toBe(429);
    const seconds = Number(came.headers()["retry-after"]);
    if (seconds >= LONG_ENOUGH_S) {
      response = came;
      break;
    }
    await expect(refused).toBeVisible();
    await expect(refused).toBeHidden({ timeout: (seconds + 5) * 1000 });
  }
  if (response === null) {
    throw new Error("no question was refused with enough of its wait left");
  }

  // The refusal itself: how long to wait, in whole seconds, and the model asked.
  const header = response.headers()["retry-after"] ?? "";
  expect(header).toMatch(/^\d+$/);
  const wait = Number(header);
  expect(wait).toBeLessThanOrEqual(60);
  expect(await response.json()).toMatchObject({
    retry_after: wait,
    model: RATIONED,
  });

  // The page's words, with the time left.
  await expect(refused).toBeVisible();
  await expect(timer).toHaveText(/^in (1 minute|\d{1,2} seconds)$/);
  // The question was never asked: it is back in the box, and can be sent again at once.
  await expect(chat.box).toHaveValue(question);
  await expect(chat.send).toBeEnabled();
  await expect(chat.questions).toHaveCount(answered);
  await expect(chat.answers).toHaveCount(answered);

  // The time left goes down: the page looks at the clock every second.
  const reading = (await timer.textContent()) ?? "";
  await expect(timer).not.toHaveText(reading);
  await expect(timer).toHaveText(/^in \d{1,2} seconds$/);

  // Dismiss takes it away, and the keyboard goes to the question in the box.
  await page.getByRole("button", { name: "Dismiss" }).click();
  await expect(refused).toBeHidden();
  await expect(timer).toHaveCount(0);
  await expect(chat.box).toBeFocused();
  await expect(chat.box).toHaveValue(question);
});
