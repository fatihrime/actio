import { describe, expect, it } from "vitest";

import { atTime, timeLeft } from "./time";

// The tests' time zone is UTC (vitest.config.mts). Between a time and its "AM" or
// "PM" the browser puts a space that doesn't break, which `\s` matches.
const NOON = new Date("2026-11-10T12:00:00Z"); // a Tuesday

describe("atTime", () => {
  it("says a moment of today by its time alone", () => {
    const later = new Date("2026-11-10T14:30:00Z");

    expect(atTime(later, NOON, "en-US")).toMatch(/^at 2:30\sPM$/);
    expect(atTime(later, NOON, "tr-TR")).toBe("at 14:30");
  });

  it("says a moment of another day with its day", () => {
    const monday = new Date("2026-11-16T00:00:00Z");
    const tonight = new Date("2026-11-11T00:30:00Z");

    expect(atTime(monday, NOON, "en-US")).toMatch(
      /^on Mon, Nov 16, 12:00\sAM$/,
    );
    expect(atTime(monday, NOON, "de-DE")).toBe("on Mon, Nov 16, 00:00");
    // After midnight is another day, however soon it is.
    expect(atTime(tonight, NOON, "en-GB")).toBe("on Wed, Nov 11, 00:30");
  });

  it("uses the words of the page and the clock of the reader", () => {
    const later = new Date("2026-11-12T09:05:00Z");

    // A Turkish reader's day isn't "Per" here: the page around it is in English.
    expect(atTime(later, NOON, "tr-TR")).toBe("on Thu, Nov 12, 09:05");
  });
});

describe("timeLeft", () => {
  const SECOND = 1000;
  const MINUTE = 60 * SECOND;
  const HOUR = 60 * MINUTE;
  const DAY = 24 * HOUR;

  it.each([
    [1, "1 second"],
    [SECOND, "1 second"],
    [SECOND + 1, "2 seconds"],
    [59 * SECOND, "59 seconds"],
    [MINUTE, "1 minute"],
    [MINUTE + 1, "2 minutes"],
    [59 * MINUTE, "59 minutes"],
    [59 * MINUTE + 1, "1 hour"],
    [HOUR + MINUTE, "1 hour 1 minute"],
    [5 * HOUR, "5 hours"],
    [5 * HOUR - 1, "5 hours"],
    [5 * HOUR - MINUTE, "4 hours 59 minutes"],
    [47 * HOUR + 30 * MINUTE, "47 hours 30 minutes"],
    [48 * HOUR, "2 days"],
    [5 * DAY + 7 * HOUR, "5 days 7 hours"],
    [5 * DAY + 6 * HOUR + 1, "5 days 7 hours"],
  ])("says %d ms as %s", (ms, words) => {
    expect(timeLeft(ms)).toBe(words);
  });

  it("never says that no time is left", () => {
    expect(timeLeft(0)).toBe("1 second");
    expect(timeLeft(-5000)).toBe("1 second");
  });
});
