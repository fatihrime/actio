// The proxy compresses what the API answers as JSON, and leaves an answer's stream as
// it is (ADR 0004, revision "the proxy compresses what the API answers as JSON"). What
// only the whole stack shows: the headers the browser was given, the sizes it counted
// on the wire, and a page that reads its conversation compressed. Which responses the
// proxy's config takes is held by deploy/tests/test_compose_policy.py.
import { expect, test } from "./chat";

/** About 300 words of plain text, which the fake model echoes after "Echo: ". The
 * conversation's JSON holds them twice, far over the 512 bytes under which nothing is
 * compressed. */
const QUESTION = Array.from(
  { length: 16 },
  (_, index) =>
    `On ride ${String(index + 1)} the Velora Cycles club went along the river to the old mill, and back before noon.`,
).join(" ");

/** What the browser counted of the response at `path`, from its Resource Timing
 * entries: the body's size on the wire, and decoded. */
interface Sizes {
  encoded: number;
  decoded: number;
}

test("the API's JSON comes compressed, and an answer's stream as it is", async ({
  chat,
  page,
}) => {
  await chat.open();
  await expect(chat.box).toBeVisible(); // the page has its session
  const asking = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().endsWith("/turns"),
  );

  await chat.ask(QUESTION);

  // `allHeaders()`, Playwright's complete list: the same call says `gzip` below.
  const asked = await asking;
  const streamed = await asked.allHeaders();
  expect(streamed["content-type"]).toMatch(/^text\/event-stream/);
  expect(streamed["content-encoding"]).toBeUndefined();
  await expect(chat.answers).toHaveText(`Echo: ${QUESTION}`);
  await chat.answered();
  await expect(page).toHaveURL(/\/c\//);

  // A reload reads the conversation, as the page reads every stored one.
  const conversation = new URL(asked.url()).pathname.replace(/\/turns$/, "");
  const reading = page.waitForResponse(
    (response) =>
      response.request().method() === "GET" &&
      new URL(response.url()).pathname === conversation,
  );
  await page.reload();
  const read = await reading;
  const headers = await read.allHeaders();
  expect(headers["content-encoding"]).toBe("gzip");
  expect(headers.vary).toMatch(/accept-encoding/i);
  // The browser decoded it: the answer is there, whole.
  await expect(chat.answers).toHaveText(`Echo: ${QUESTION}`);

  // The browser's own account of the read: less than half as large on the wire.
  const counted = () =>
    page.evaluate(
      (path) =>
        performance
          .getEntriesByType("resource")
          .filter((entry) => new URL(entry.name).pathname === path)
          .map((entry): Sizes => {
            const timing = entry as PerformanceResourceTiming;
            return {
              encoded: timing.encodedBodySize,
              decoded: timing.decodedBodySize,
            };
          }),
      conversation,
    );
  await expect.poll(async () => (await counted()).length).toBeGreaterThan(0);
  for (const { encoded, decoded } of await counted()) {
    expect(decoded).toBeGreaterThan(2 * QUESTION.length); // the question and its echo
    expect(encoded).toBeGreaterThan(0); // counted, not left out
    expect(encoded).toBeLessThan(decoded / 2);
  }

  // A client that doesn't take gzip, with the browser's session, gets it as it is.
  const plain = await page.request.get(conversation, {
    headers: { "accept-encoding": "identity" },
  });
  expect(plain.status()).toBe(200);
  expect(plain.headers()["content-encoding"]).toBeUndefined();
  // Told as well that the answer depends on what a client takes, for a cache between.
  expect(plain.headers().vary).toMatch(/accept-encoding/i);
  // Whether its title is on its way changes by itself, 15 s after the first answer
  // (ADR 0025): everything else is what the page read.
  const asIs = (await plain.json()) as Record<string, unknown>;
  const asRead = (await read.json()) as Record<string, unknown>;
  expect({ ...asIs, title_pending: null }).toEqual({
    ...asRead,
    title_pending: null,
  });
});
