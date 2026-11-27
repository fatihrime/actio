// A model that answers with nothing: the page says so, and asks again when told to
// (ADR 0015, revision "a turn that left no answer says so"). What only the whole stack
// shows: what a real server's empty answer leaves behind, in the stream the page reads
// and in the conversation it reads again. And a model that thought until it reached the
// longest a single answer may be, which the server tells from the end of the window
// (revision "the page says which limit an answer reached"). The rule's variants are the
// component tests' (components/chat/chat-app.test.tsx).
import { expect, test } from "./chat";

const NO_ANSWER = "The model gave no answer.";
/** What the page says of a model asked to think, that thought all a single answer may
 * hold (ADR 0015, revision "the page says which limit an answer reached"). */
const THOUGHT_TOO_LONG =
  "The model was still thinking when it reached the longest a single answer may be, and wrote no answer. Try again, or turn off “Think before answering” first.";

test("a question that a model answered with nothing says so, also after a reload, and is asked again", async ({
  chat,
  page,
}) => {
  // The stack's quiet model names conversations, and says nothing of its own. This
  // test was written before the page could choose a model (e2e/picker.spec.ts), so the
  // first request for an answer is given it on its way out. The requests after that go
  // as the page made them.
  let toTheQuietModel = true;
  await page.route("**/api/conversations/*/turns", async (route) => {
    const request = route.request();
    if (!toTheQuietModel || request.method() !== "POST") {
      await route.continue();
      return;
    }
    toTheQuietModel = false;
    const asked = JSON.parse(request.postData() ?? "{}") as Record<
      string,
      unknown
    >;
    await route.continue({
      postData: JSON.stringify({ ...asked, model: "namer" }),
    });
  });
  await chat.open();

  await chat.ask("Anyone there?");

  await expect(page.getByText(NO_ANSWER)).toBeVisible();
  await chat.answered();
  await expect(chat.answers).toHaveCount(0);

  await page.reload();

  // The server kept the question, and no answer.
  await expect(chat.questions).toHaveText("Anyone there?");
  await expect(page.getByText(NO_ANSWER)).toBeVisible();
  await expect(chat.answers).toHaveCount(0);

  await page.getByRole("button", { name: "Try again" }).click();

  await expect(chat.answers).toHaveText("Echo: Anyone there?");
  await chat.answered();
  await expect(page.getByText(NO_ANSWER)).toBeHidden();
});

test("a model still thinking at the longest an answer may be says so, also after a reload, and offers another try", async ({
  chat,
  page,
}) => {
  // More words of thought than a single answer may hold on the stack, 2,048
  // (deploy/e2e/config/models.toml). The fake cuts its thought there, writes no
  // answer, and ends for its length, far from the window's end.
  const thought = Array.from({ length: 2_100 }, () => "hmm").join(" ");
  const kept = chat.answers.locator("details");
  const tryAgain = page.getByRole("button", { name: "Try again" });
  await chat.open();
  await page.getByLabel("Think before answering").check();

  await chat.ask(`[[think: ${thought}]] How many words was that?`);

  // Two thousand words of thought take longer than the few seconds a wait has by
  // itself, when the stack is busy with the other tests.
  await expect(page.getByText(THOUGHT_TOO_LONG)).toBeVisible({
    timeout: 20_000,
  });
  await chat.answered();
  await expect(kept.locator("summary")).toHaveText("Thought before answering");
  await expect(tryAgain).toBeVisible();

  await page.reload();

  await expect(kept.locator("summary")).toHaveText("Thought before answering");
  await expect(page.getByText(THOUGHT_TOO_LONG)).toBeVisible();
  await expect(tryAgain).toBeVisible();
});
