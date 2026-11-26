// An answer that waits its turn at its model's gate says behind how many other answers
// it waits, in the place of "Waiting for the model…" (ADR 0023, revision "a turn says
// that it waits, and behind how many answers"). What only the whole stack shows: a real
// gate in the API in front of the fake's server, the number in the turn's stream through
// the proxy, and a reload that joins that stream in Valkey from its start. The variants
// are the component tests' (components/chat/waits.test.tsx, lib/chat/waits.test.ts).
import { expect, test } from "./chat";

/** The stack's model behind a gate that lets one answer through at a time
 * (deploy/e2e/config/models.toml). */
const QUEUED = "queued";

/** Twenty words: at half a second each, an answer still being written after a second
 * question, a look at it and a reload, with seconds to spare. */
const TWENTY =
  "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty";

/** What the page says of an answer that waits behind others. The number is never one
 * this test counts on: another run of it may be at the same gate. */
const BEHIND = /^Waiting behind \d+ other answers?…$/;

/** Long enough for this test's first answer, about ten seconds, and for the answers of
 * another run of it that are before this one's at the gate, as long each. */
const IN_TURN = { timeout: 45_000 };

test("an answer asked while another is written at its model's gate says behind how many it waits, also after a reload, and is written once its turn comes", async ({
  chat,
  page,
}) => {
  test.setTimeout(90_000);
  // The page has no way to choose a model: every request for a turn it makes is given
  // the name of the one behind the gate on its way out. Nothing else of a request is
  // changed.
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
      postData: JSON.stringify({ ...asked, model: QUEUED }),
    });
  });
  await chat.open();

  // The first answer is written slowly: the gate's one place is its own.
  await chat.ask(`[[pace: 0.5]] ${TWENTY}`);
  await expect(chat.answers).toContainText("Echo: one", IN_TURN);
  const first = page.url();

  // A second question, in a new conversation: a guest may have two answers at once.
  await chat.newChat.click();
  await expect(chat.questions).toHaveCount(0);
  await chat.ask("Is it my turn yet?");

  const waiting = chat.answers.getByText(BEHIND);
  await expect(waiting).toBeVisible();
  await expect(page.getByText("Waiting for the model…")).toHaveCount(0);
  await expect(page).toHaveURL(/\/c\//);
  expect(page.url()).not.toBe(first);

  // A reload reads the turn's stream again, from its start, and says the same.
  await page.reload();
  await expect(chat.questions).toHaveText(["Is it my turn yet?"]);
  await expect(waiting).toBeVisible();

  // Once the answer before it has ended, it is written, with its own words.
  await expect(chat.answers).toHaveText("Echo: Is it my turn yet?", IN_TURN);
  await chat.answered();
  await expect(page.getByText(/^Waiting/)).toHaveCount(0);
});
