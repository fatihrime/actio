// An answer whose model was still writing when its server's time for a whole answer was
// over (ADR 0028, revision "the longest an answer may take"): the model client cuts it
// there, the turn completes at that limit, is paid for by the API's guess, and the page
// says so. What only the whole stack shows: a real server's stream, the client's own
// clock, what the page reads of it, and the conversation it reads again. The rule's
// variants are the component tests' (components/chat/chat-app.test.tsx).
import { expect, test } from "./chat";

/** What the page says of an answer cut at the longest a single answer may take. */
const SAID =
  "The answer took the longest a single answer may take, and stops here.";
/** Thirty words, each its own, which the fake echoes one every half second: the server
 * of the stack's "hurried" model is given 3 seconds for an answer
 * (deploy/e2e/config/models.toml), so only the first few of them come. */
const WORDS = [
  "saddle",
  "pedal",
  "chain",
  "spoke",
  "rim",
  "tyre",
  "valve",
  "brake",
  "lever",
  "cable",
  "gear",
  "crank",
  "hub",
  "fork",
  "frame",
  "stem",
  "bell",
  "basket",
  "light",
  "helmet",
  "pump",
  "patch",
  "wrench",
  "bolt",
  "washer",
  "bearing",
  "sprocket",
  "derailleur",
  "mudguard",
  "kickstand",
];

test("an answer still being written when its server's time is over stops there, says so and what it cost, also after a reload, with no other try", async ({
  chat,
  page,
}) => {
  // The stack's hurried model is the fake behind a server given 3 seconds for an
  // answer. The page has no way to choose it, so the first request for an answer is
  // given it on its way out. The requests after that go as the page made them.
  let toTheHurriedModel = true;
  await page.route("**/api/conversations/*/turns", async (route) => {
    const request = route.request();
    if (!toTheHurriedModel || request.method() !== "POST") {
      await route.continue();
      return;
    }
    toTheHurriedModel = false;
    const asked = JSON.parse(request.postData() ?? "{}") as Record<
      string,
      unknown
    >;
    await route.continue({
      postData: JSON.stringify({ ...asked, model: "hurried" }),
    });
  });
  const took = page.getByRole("list", { name: "What this answer took" });
  const cost = took.getByRole("listitem").filter({ hasText: /credits?$/ });
  const tryAgain = page.getByRole("button", { name: "Try again" });
  await chat.open();

  await chat.ask(`[[pace: 0.5]] ${WORDS.join(" ")}`);

  // The answer can't end before its 3 seconds are over: that leaves a wait's own few
  // seconds little room, when the stack is busy with the other tests.
  await expect(page.getByText(SAID)).toBeVisible({ timeout: 20_000 });
  await chat.answered();
  await expect(chat.answers).toHaveCount(1);
  await expect(chat.answers).toContainText(`Echo: ${WORDS[0] ?? ""}`);
  await expect(chat.answers).not.toContainText(WORDS.at(-1) ?? "");
  await expect(tryAgain).toHaveCount(0);
  // Counted by the API's guess: a stream that was closed sends no count.
  await expect(cost).toHaveText(/^\d[\d,]* credits?$/);
  const answer = await chat.answers.textContent();
  const credits = await cost.textContent();

  await page.reload();

  // The server kept the answer as far as it got, and how its turn ended.
  await expect(chat.answers).toHaveText(answer ?? "");
  await expect(page.getByText(SAID)).toBeVisible();
  await expect(cost).toHaveText(credits ?? "");
  await expect(tryAgain).toHaveCount(0);
});
