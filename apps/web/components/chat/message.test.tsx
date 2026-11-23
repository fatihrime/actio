// What turns markdown into a page is fetched apart from the page (message.tsx). This
// file is about the day it can't be: the installation was upgraded while the page was
// open, and the piece the page asks for is no longer there.
import { act, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import type { ChatMessage } from "@/lib/chat/messages";

import { Message } from "./message";

vi.mock("./markdown", () => {
  throw new Error("Failed to load chunk");
});

const ANSWER: ChatMessage = {
  id: "01900000-0000-7000-8000-000000000002",
  role: "assistant",
  parts: [
    { type: "reasoning", text: "A *bike* has parts." },
    { type: "text", text: "The **saddle**." },
  ],
};

test("an answer is shown as it was written when what formats it can't be fetched", async () => {
  render(<Message message={ANSWER} />);

  // Until the fetch has failed, the answer waits for it. So wait for that, and for the
  // page to have taken it in.
  await act(async () => {
    await vi.dynamicImportSettled();
  });

  expect(screen.getByText("The **saddle**.")).toBeVisible();
  expect(screen.getByText("A *bike* has parts.")).toBeInTheDocument();
});

// A piece of an answer begins a moment before its first words. Until something of the
// answer is drawn, it goes on saying that it waits for the model (ADR 0015, revision
// "an answer waits for the model until it has words"). What formats an answer can't be
// fetched in this file, so its words are shown as written, without a wait for that.

/** An answer that is still arriving, with these parts so far. */
async function arriving(parts: ChatMessage["parts"]): Promise<HTMLElement> {
  render(<Message message={{ ...ANSWER, parts }} writing />);
  await act(async () => {
    await vi.dynamicImportSettled();
  });
  return screen.getByRole("article", { name: "Actio" });
}

test.each([
  { what: "has begun and has no words yet", text: "" },
  { what: "is blank space so far", text: "\n\n " },
])(
  "an arriving answer whose text $what still waits for the model",
  async ({ text }) => {
    const answer = await arriving([{ type: "text", text }]);

    // The wait line alone: no empty piece stands beside it, or in its place.
    expect(answer.textContent).toBe("Waiting for the model…");
    expect(answer.children).toHaveLength(1);
  },
);

test("an arriving answer's first words take the wait's place", async () => {
  const answer = await arriving([{ type: "text", text: "The " }]);

  expect(answer.textContent).toBe("The ");
});

test("a thought that has begun says so, before it has words", async () => {
  const answer = await arriving([{ type: "reasoning", text: "" }]);

  expect(screen.getByText("Thinking…")).toBeVisible();
  expect(answer.querySelector("details")).toHaveAttribute("open");
  expect(screen.queryByText("Waiting for the model…")).not.toBeInTheDocument();
});

test("a thought stays open until the text after it has words, and folds away as they come", async () => {
  const thought = { type: "reasoning" as const, text: "A bike has parts." };
  const page = render(
    <Message
      message={{ ...ANSWER, parts: [thought, { type: "text", text: "" }] }}
      writing
    />,
  );
  await act(async () => {
    await vi.dynamicImportSettled();
  });
  const answer = screen.getByRole("article", { name: "Actio" });

  // The text has begun: nothing of it is drawn, and the thought is as it was.
  expect(screen.getByText("Thinking…")).toBeVisible();
  expect(answer.querySelector("details")).toHaveAttribute("open");
  expect(answer.children).toHaveLength(1);
  expect(screen.queryByText("Waiting for the model…")).not.toBeInTheDocument();

  page.rerender(
    <Message
      message={{ ...ANSWER, parts: [thought, { type: "text", text: "The " }] }}
      writing
    />,
  );

  expect(screen.getByText("Thought before answering")).toBeVisible();
  expect(answer.querySelector("details")).not.toHaveAttribute("open");
  expect(answer.children).toHaveLength(2);
  expect(answer.lastElementChild?.textContent).toBe("The ");
});
