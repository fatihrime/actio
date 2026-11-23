// A model that answers with nothing: the page says so, and asks again when told to
// (ADR 0015, revision "a turn that left no answer says so"). What only the whole stack
// shows: what a real server's empty answer leaves behind, in the stream the page reads
// and in the conversation it reads again. The rule's variants are the component
// tests' (components/chat/chat-app.test.tsx).
import { expect, test } from "./chat";

const NO_ANSWER = "The model gave no answer.";

test("a question that a model answered with nothing says so, also after a reload, and is asked again", async ({
  chat,
  page,
}) => {
  // The stack's quiet model names conversations, and says nothing of its own. The page
  // has no way to choose it, so the first request for an answer is given it on its way
  // out. The requests after that go as the page made them.
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
