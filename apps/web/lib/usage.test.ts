import { describe, expect, it } from "vitest";

import { ApiProblem } from "@/lib/api/client";
import type { Problem } from "@/lib/api/client";
import { usageWindow } from "@/tests/fake-api";
import { recorded } from "@/tests/recorded";

import { passedIn, reachedBy, shareUsed } from "./usage";

const NOW = Date.parse("2026-11-10T12:00:00Z");

/** The refusal the API was recorded giving at a usage limit, with some of it changed. */
function refusal(changes: Record<string, unknown> = {}): ApiProblem {
  const problem = JSON.parse(recorded("limit_reached.json")) as Problem;
  return new ApiProblem({ ...problem, ...changes });
}

describe("reachedBy", () => {
  it("reads the API's refusal at a usage limit", () => {
    expect(reachedBy(refusal(), NOW)).toEqual({
      window: "session",
      resetsAt: new Date("2026-11-10T17:00:00Z"),
      free: ["local"],
    });
  });

  it("counts the wait from this browser's clock, whatever the server's says", () => {
    const fastClock = NOW + 7 * 60_000;

    const reached = reachedBy(refusal(), fastClock);

    // Five hours from now, not the moment named by a server whose clock differs.
    expect(reached?.resetsAt).toEqual(new Date("2026-11-10T17:07:00Z"));
  });

  it("takes a 429 that names no window for the limit on how often things are tried", () => {
    const tooOften = new ApiProblem({
      type: "about:blank",
      title: "Too Many Requests",
      status: 429,
      detail: "Too many messages. Try again in 30 seconds.",
      retry_after: 30,
    });

    expect(reachedBy(tooOften, NOW)).toBeNull();
  });

  it("takes no model having budget left for a question for no usage limit", () => {
    // The API's refusal then: a wait and the model asked, and no window.
    const noBudget = new ApiProblem(
      JSON.parse(recorded("no_budget.json")) as Problem,
    );

    expect(reachedBy(noBudget, NOW)).toBeNull();
  });

  it("doesn't wait out a question that is too large for the plan", () => {
    const tooLarge = refusal({ retry_after: undefined, resets_at: null });

    expect(reachedBy(tooLarge, NOW)).toBeNull();
  });

  it("leaves everything else alone", () => {
    expect(reachedBy(refusal({ status: 503 }), NOW)).toBeNull();
    expect(reachedBy(refusal({ window: "month" }), NOW)).toBeNull();
    expect(reachedBy(new TypeError("fetch failed"), NOW)).toBeNull();
    expect(reachedBy(refusal({ alternatives: "local" }), NOW)?.free).toEqual(
      [],
    );
  });
});

describe("passedIn", () => {
  it("reads a usage warning", () => {
    expect(passedIn({ window: "week", percent: 75 })).toEqual({
      window: "week",
      percent: 75,
    });
  });

  it("makes nothing of what isn't one", () => {
    expect(passedIn(null)).toBeNull();
    expect(passedIn("session")).toBeNull();
    expect(passedIn({ window: "month", percent: 50 })).toBeNull();
    expect(passedIn({ window: "session", percent: "50" })).toBeNull();
    expect(passedIn({ window: "session" })).toBeNull();
  });
});

describe("shareUsed", () => {
  const of = (used: number, limit = 1000, reserved = 0) =>
    shareUsed(usageWindow("session", { used, limit, reserved }));

  it("says a share in whole percent, rounded down", () => {
    expect(of(0)).toEqual({ percent: 0, label: "0%" });
    expect(of(429)).toEqual({ percent: 42.9, label: "42%" });
    expect(of(999).label).toBe("99%"); // not full until it is
    expect(of(1000)).toEqual({ percent: 100, label: "100%" });
  });

  it("doesn't call a little nothing", () => {
    expect(of(118, 25_000).label).toBe("under 1%");
  });

  it("stops at full, though a little more can be used than the limit", () => {
    expect(of(1040)).toEqual({ percent: 100, label: "100%" });
  });

  it("leaves out what is only held for an answer being written", () => {
    expect(of(100, 1000, 600).label).toBe("10%");
  });

  it("calls a window that allows nothing full", () => {
    expect(of(0, 0)).toEqual({ percent: 100, label: "100%" });
  });
});
