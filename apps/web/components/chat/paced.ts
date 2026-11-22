import { useEffect, useRef, useState } from "react";

/** How often an arriving answer's text is drawn at the most, in milliseconds: about
 * twenty times a second, where the chat library hands on a new text at each piece that
 * comes in, and the page drew it at every frame (ADR 0016, revision "an arriving answer
 * is drawn at most every 50 ms"). */
export const DRAWN_EVERY_MS = 50;

/** The text to draw of `text`, which may still be `arriving`.
 *
 * Whole while it doesn't arrive. While it arrives, the text last handed on, until
 * `DRAWN_EVERY_MS` have passed since that was on the page; then the newest. The wait
 * counts from when it was on the page, not from when it was handed on, so a draw that
 * takes long doesn't use up the wait, and a slow device is never drawing back to back.
 * Whole again in the very render in which it stops arriving.
 *
 * Only an earlier state of the same text is drawn in its place: one that doesn't begin
 * with what was last handed on is handed on as it is. */
export function usePaced(text: string, arriving: boolean): string {
  // What was last handed on. The text the component starts with is the first.
  const [drawn, setDrawn] = useState(text);
  // Text that doesn't arrive is handed on as it is, so it is what was last handed on:
  // should it arrive again, it goes on from there, and not from an older state.
  if (!arriving && drawn !== text) {
    setDrawn(text);
  }
  // When what was last handed on was on the page, by the clock. Read with `Date.now()`,
  // so that a test sets it (tests/clock.ts).
  const shownAt = useRef(0);
  useEffect(() => {
    shownAt.current = Date.now();
  }, [drawn]);

  useEffect(() => {
    if (!arriving || text === drawn) {
      return;
    }
    let timer: ReturnType<typeof setTimeout> | undefined;
    const look = () => {
      const since = Date.now() - shownAt.current;
      // A clock that was put back says nothing of how long ago that was.
      if (since >= 0 && since < DRAWN_EVERY_MS) {
        timer = setTimeout(look, DRAWN_EVERY_MS - since);
        return;
      }
      // Handed on from here when the wait is already over, not by a timer of 0 ms: one
      // set anew at each piece could be put off for as long as pieces keep coming.
      setDrawn(text);
    };
    look();
    return () => {
      clearTimeout(timer);
    };
  }, [text, arriving, drawn]);

  return arriving && text.startsWith(drawn) ? drawn : text;
}
