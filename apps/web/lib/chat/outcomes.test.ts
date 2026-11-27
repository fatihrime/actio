import { describe, expect, it } from "vitest";

import { ApiProblem } from "@/lib/api/client";
import type { Model, Problem } from "@/lib/api/client";
import { freeModel, model, secondModel } from "@/tests/fake-api";
import { recorded } from "@/tests/recorded";

import { meetsInShort, refusedForNow } from "./outcomes";

const NOW = Date.parse("2026-11-10T12:00:00Z");

/** A refusal the API was recorded giving, such as `no_budget.json`, with some of it
 * changed. */
function refusal(
  name: string,
  changes: Record<string, unknown> = {},
): ApiProblem {
  const problem = JSON.parse(recorded(name)) as Problem;
  return new ApiProblem({ ...problem, ...changes });
}

/** A refusal as the API words one that no file records. */
function problem(
  status: number,
  detail: string,
  extra: Record<string, unknown> = {},
): ApiProblem {
  return new ApiProblem({
    type: "about:blank",
    title: "",
    status,
    detail,
    ...extra,
  });
}

describe("refusedForNow", () => {
  it("reads the API's refusal of a question no model has budget left for", () => {
    // It says 60 seconds, and names the model asked.
    expect(refusedForNow(refusal("no_budget.json"), NOW)).toBe(NOW + 60_000);
  });

  it("counts the wait from the moment the refusal arrived", () => {
    const later = NOW + 7 * 60_000;

    expect(
      refusedForNow(refusal("no_budget.json", { retry_after: 15 }), later),
    ).toBe(later + 15_000);
  });

  it("takes a wait of a day as it is", () => {
    expect(
      refusedForNow(refusal("no_budget.json", { retry_after: 86_400 }), NOW),
    ).toBe(NOW + 86_400_000);
  });

  it("leaves the other 429s alone", () => {
    // Too many messages: a wait, and no model.
    expect(
      refusedForNow(
        problem(
          429,
          "Too many messages in a short time. Try again in 40 seconds.",
          { retry_after: 40 },
        ),
        NOW,
      ),
    ).toBeNull();
    // Too many answers at once: no wait.
    expect(
      refusedForNow(
        problem(429, "2 answers are already being written for you.", {
          at_once: 2,
        }),
        NOW,
      ),
    ).toBeNull();
    // A usage limit names a window, whatever else it names.
    expect(
      refusedForNow(refusal("limit_reached.json", { model: "standard" }), NOW),
    ).toBeNull();
    // So does a question too large for the plan, with no wait.
    expect(
      refusedForNow(
        refusal("limit_reached.json", {
          retry_after: undefined,
          model: "standard",
        }),
        NOW,
      ),
    ).toBeNull();
    // A window, even one that is no usage window's name.
    expect(
      refusedForNow(refusal("no_budget.json", { window: "month" }), NOW),
    ).toBeNull();
    // A model, and no wait.
    expect(
      refusedForNow(refusal("no_budget.json", { retry_after: undefined }), NOW),
    ).toBeNull();
  });

  it("takes a window of nothing for none", () => {
    expect(
      refusedForNow(refusal("no_budget.json", { window: null }), NOW),
    ).toBe(NOW + 60_000);
  });

  it("wants a model that is named", () => {
    expect(
      refusedForNow(refusal("no_budget.json", { model: null }), NOW),
    ).toBeNull();
    expect(
      refusedForNow(refusal("no_budget.json", { model: 1 }), NOW),
    ).toBeNull();
  });

  it("leaves everything else alone", () => {
    // A model can't answer, and says which: no 429.
    expect(
      refusedForNow(refusal("no_budget.json", { status: 503 }), NOW),
    ).toBeNull();
    expect(refusedForNow(new TypeError("fetch failed"), NOW)).toBeNull();
    expect(refusedForNow(null, NOW)).toBeNull();
  });
});

describe("meetsInShort", () => {
  /** The models on offer: the default, which names "Second model" to stand in for it,
   * Local, and "Second model". */
  const OFFERED = [model({ then: ["second"] }), freeModel(), secondModel()];

  /** What the row of the default says, the list having said `changes` of it. */
  function said(changes: Partial<Model>): string | null {
    return meetsInShort(model({ then: ["second"], ...changes }), OFFERED);
  }

  it.each<[string, Partial<Model>, string | null]>([
    ["isn't downloaded", { status: "missing" }, "Isn't downloaded yet"],
    ["can't be reached", { status: "unreachable" }, "Can't be reached"],
    [
      "is refused for a second",
      { asked_first: null, retry_after: 1 },
      "At its provider's limit for under a minute",
    ],
    [
      "is refused for 59 seconds",
      { asked_first: null, retry_after: 59 },
      "At its provider's limit for under a minute",
    ],
    [
      "is refused for 60 seconds",
      { asked_first: null, retry_after: 60 },
      "At its provider's limit for about 1 minute",
    ],
    [
      "is refused for an hour",
      { asked_first: null, retry_after: 3600 },
      "At its provider's limit for about 1 hour",
    ],
    [
      "is refused for a day",
      { asked_first: null, retry_after: 86_400 },
      "At its provider's limit for about 24 hours",
    ],
    ["is given to it, which keeps failing", { failing: true }, "Keeps failing"],
    [
      "is given to another model first",
      { asked_first: "second" },
      "Second model answers for now",
    ],
    [
      "is given first to a model the list doesn't have",
      { asked_first: "elsewhere" },
      "elsewhere answers for now",
    ],
    [
      "is given to another model first, which keeps failing",
      { asked_first: "second", failing: true },
      "Second model answers for now, and keeps failing",
    ],
    ["is given to it, and nothing is in the way", {}, null],
    // The list says no such thing of a model: nothing of it is made up.
    ["is given to nobody, and nothing says why", { asked_first: null }, null],
  ])("says of a model whose shortest question %s: %s", (_, changes, words) => {
    expect(said(changes)).toBe(words);
  });

  it("says why a model can't answer before anything else", () => {
    expect(said({ status: "unreachable", retry_after: 60 })).toBe(
      "Can't be reached",
    );
    expect(said({ status: "missing", failing: true })).toBe(
      "Isn't downloaded yet",
    );
    expect(said({ status: "missing", asked_first: "second" })).toBe(
      "Isn't downloaded yet",
    );
  });

  it("says a wait before a model that keeps failing, and before another model that answers", () => {
    expect(said({ retry_after: 30, failing: true })).toBe(
      "At its provider's limit for under a minute",
    );
    expect(said({ retry_after: 30, asked_first: "second" })).toBe(
      "At its provider's limit for under a minute",
    );
  });

  it("says that a model keeps failing of the model that does: the row's own, or the other by its title", () => {
    expect(said({ failing: true })).toBe("Keeps failing");
    expect(said({ failing: true, asked_first: "second" })).toBe(
      "Second model answers for now, and keeps failing",
    );
    expect(said({ failing: true, asked_first: "elsewhere" })).toBe(
      "elsewhere answers for now, and keeps failing",
    );
  });

  it("names the other model by its title, from the models it is given", () => {
    const renamed = [model(), secondModel({ title: "Renamed" })];

    expect(meetsInShort(model({ asked_first: "second" }), renamed)).toBe(
      "Renamed answers for now",
    );
    expect(meetsInShort(model({ asked_first: "second" }), [])).toBe(
      "second answers for now",
    );
  });
});
