// The browser's clock, for a test that shows a time of day or waits for a moment.
import { vi } from "vitest";

/** The moment the recorded turns happened at, and the fake API's answers are dated:
 * a Tuesday, at noon in the tests' time zone (UTC, see vitest.config.mts). */
export const NOON = "2026-11-10T12:00:00Z";

export interface Clock {
  /** Move the clock on by so many milliseconds. */
  advance: (ms: number) => void;
}

/** Set the clock for the running test. Only what the page reads off the clock
 * changes: timers run as ever, in real time, so the page is still driven as a person
 * would drive it. A page that waits for a moment notices that the clock was moved
 * when it next looks, which is after a second at most. */
export function clockAt(moment: string = NOON): Clock {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(moment));
  return {
    advance: (ms) => {
      vi.setSystemTime(Date.now() + ms);
    },
  };
}

export const SECOND = 1000;
export const MINUTE = 60 * SECOND;
export const HOUR = 60 * MINUTE;
