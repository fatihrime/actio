// Moments and lengths of time, in words for a person.
import { useEffect, useState } from "react";

/** When a moment is, as its reader's clock shows it: "at 2:30 PM" or "at 14:30" when
 * it is today, and with the day when it is another one, "on Mon, Nov 16, 00:00".
 *
 * The words are English, like the rest of the page. Whether the clock counts twelve
 * hours or twenty-four is the reader's own, taken from their browser's language. */
export function atTime(
  moment: Date,
  now: Date,
  language: string = navigator.language,
): string {
  const today = moment.toDateString() === now.toDateString();
  const said = new Intl.DateTimeFormat("en", {
    ...(today ? {} : { weekday: "short", month: "short", day: "numeric" }),
    hour: "numeric",
    minute: "2-digit",
    hourCycle: new Intl.DateTimeFormat(language, {
      hour: "numeric",
    }).resolvedOptions().hourCycle,
  }).format(moment);
  return today ? `at ${said}` : `on ${said}`;
}

/** Which day a moment was, as its reader's clock shows it: "today at 2:30 PM" or "today
 * at 14:30" when it is today, as `atTime` says it, and otherwise the day with its year
 * and without a time, "on Nov 16, 2026". For what happened long ago as much as an hour
 * ago, such as when an API key was last used. */
export function onDay(
  moment: Date,
  now: Date,
  language: string = navigator.language,
): string {
  if (moment.toDateString() === now.toDateString()) {
    return `today ${atTime(moment, now, language)}`;
  }
  const day = new Intl.DateTimeFormat("en", {
    month: "short",
    day: "numeric",
    year: "numeric",
  }).format(moment);
  return `on ${day}`;
}

/** The time by this browser's clock, read again every `everyMs` milliseconds: for
 * what is shown as it stands to now, such as the time left until something. */
export function useNow(everyMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => {
      setNow(Date.now());
    }, everyMs);
    return () => {
      clearInterval(timer);
    };
  }, [everyMs]);
  return now;
}

/** How long a wait of `ms` milliseconds is: "45 seconds", "12 minutes", "4 hours 12
 * minutes", "5 days 7 hours". Rounded up, so a wait is never said to be shorter than
 * it is, and is "1 second" until it is over. */
export function timeLeft(ms: number): string {
  const seconds = Math.max(1, Math.ceil(ms / 1000));
  if (seconds < 60) {
    return count(seconds, "second");
  }
  const minutes = Math.ceil(seconds / 60);
  if (minutes < 60) {
    return count(minutes, "minute");
  }
  if (minutes < 48 * 60) {
    return both(Math.floor(minutes / 60), "hour", minutes % 60, "minute");
  }
  const hours = Math.ceil(minutes / 60);
  return both(Math.floor(hours / 24), "day", hours % 24, "hour");
}

/** How long a wait of `ms` milliseconds is, roughly: for a wait that is said once, as it
 * was known when it was read, and not counted down. "under a minute" where `timeLeft`
 * says seconds, and otherwise "about" and its words: "about 1 minute", "about 4 hours
 * 12 minutes". */
export function timeLeftRoughly(ms: number): string {
  const seconds = Math.max(1, Math.ceil(ms / 1000));
  return seconds < 60 ? "under a minute" : `about ${timeLeft(ms)}`;
}

/** How long something took that lasted `ms` milliseconds: "240 ms" under a second, to
 * the nearest ten and "10 ms" at the least; "1.2 s" under ten seconds; "42 s" under a
 * minute; then "1 min 5 s". A time that rounds up to the next step is said in that
 * step's words: 996 ms is "1.0 s", and 59.6 seconds "1 min". */
export function timeTaken(ms: number): string {
  const tens = Math.max(10, Math.round(ms / 10) * 10);
  if (tens < 1000) {
    return `${String(tens)} ms`;
  }
  const tenths = Math.round(ms / 100) / 10;
  if (tenths < 10) {
    return `${tenths.toFixed(1)} s`;
  }
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) {
    return `${String(seconds)} s`;
  }
  const minutes = `${String(Math.floor(seconds / 60))} min`;
  return seconds % 60 === 0 ? minutes : `${minutes} ${String(seconds % 60)} s`;
}

function count(amount: number, unit: string): string {
  return `${String(amount)} ${unit}${amount === 1 ? "" : "s"}`;
}

function both(
  larger: number,
  largerUnit: string,
  smaller: number,
  smallerUnit: string,
): string {
  const first = count(larger, largerUnit);
  return smaller === 0 ? first : `${first} ${count(smaller, smallerUnit)}`;
}
