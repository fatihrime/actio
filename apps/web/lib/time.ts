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
