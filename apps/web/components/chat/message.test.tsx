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

  // Until the fetch has failed, the same words stand in for the formatted answer.
  // So wait for that, and for the page to have taken it in.
  await act(async () => {
    await vi.dynamicImportSettled();
  });

  expect(screen.getByText("The **saddle**.")).toBeVisible();
  expect(screen.getByText("A *bike* has parts.")).toBeInTheDocument();
});
