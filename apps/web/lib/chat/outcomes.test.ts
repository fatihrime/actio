import { describe, expect, it } from "vitest";

import { ApiProblem } from "@/lib/api/client";
import type { Problem } from "@/lib/api/client";
import { recorded } from "@/tests/recorded";

import { refusedForNow } from "./outcomes";

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
