// The worker's entry (colouring.worker.ts): a question with an id in, that id out with
// the colours, or with what went wrong. An error that was not said would leave its
// question waiting on the page for good, and with it every count of what waits.
import { beforeEach, describe, expect, test, vi } from "vitest";

import type { Answered, Asked, Colours, Question } from "./colouring";

/** Stands in for the worker's global scope. */
const scope = {
  onmessage: null as ((event: { data: Asked }) => void) | null,
  postMessage: vi.fn<(message: Answered) => void>(),
};

const colour = vi.fn<(question: Question) => Promise<Colours>>();

const QUESTION: Question = {
  code: "const a = 1;",
  language: "ts",
  themes: ["github-light", "github-dark"],
  have: 0,
};

beforeEach(async () => {
  scope.onmessage = null;
  scope.postMessage.mockReset();
  colour.mockReset();
  vi.stubGlobal("self", scope);
  vi.doMock("./colouring", () => ({ colour }));
  vi.resetModules();
  await import("./colouring.worker");
});

/** Ask the worker, as the page does. */
function ask(id: number): void {
  expect(scope.onmessage).not.toBeNull();
  scope.onmessage?.({ data: { id, question: QUESTION } });
}

describe("the worker that colours code", () => {
  test("answers a question with its id and the colours", async () => {
    const colours: Colours = { fg: "#24292e", bg: "#fff", keep: 0, rows: [] };
    colour.mockResolvedValue(colours);

    ask(7);

    await vi.waitFor(() => {
      expect(scope.postMessage).toHaveBeenCalledExactlyOnceWith({
        id: 7,
        colours,
      });
    });
    expect(colour).toHaveBeenCalledExactlyOnceWith(QUESTION);
  });

  test("says what went wrong, with the question's id, when colouring failed", async () => {
    colour.mockRejectedValue(new Error("The grammar couldn't be fetched."));

    ask(8);

    await vi.waitFor(() => {
      expect(scope.postMessage).toHaveBeenCalledExactlyOnceWith({
        id: 8,
        error: "The grammar couldn't be fetched.",
      });
    });
  });

  test("answers each question under its own id, whichever is done first", async () => {
    let done: (colours: Colours) => void = () => undefined;
    colour
      .mockReturnValueOnce(
        new Promise<Colours>((resolve) => {
          done = resolve;
        }),
      )
      .mockRejectedValueOnce("no such theme");

    ask(1);
    ask(2);

    await vi.waitFor(() => {
      expect(scope.postMessage).toHaveBeenCalledExactlyOnceWith({
        id: 2,
        error: "no such theme",
      });
    });
    const colours: Colours = { fg: "#24292e", bg: "#fff", keep: 0, rows: [] };
    done(colours);
    await vi.waitFor(() => {
      expect(scope.postMessage).toHaveBeenLastCalledWith({ id: 1, colours });
    });
  });
});
