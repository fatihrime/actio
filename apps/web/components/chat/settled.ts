import { useEffect, useState } from "react";

import { afterPaint } from "@/lib/after-paint";

/** Whether an answer has ended and its last words have been painted: false while it
 * arrives and for one more frame after, true from then on, and from the first render
 * for an answer that isn't arriving, as a stored one.
 *
 * What the page says about an answer once it has ended, what it took and how its turn
 * ended, is drawn by this and not by the answer's end alone. In the frame in which a
 * long answer ends the page draws all of its words once more, as markdown that is
 * complete. With the line of what it took coming onto the page in that same frame,
 * Safari's engine took longer over it at the end of a long answer, 37 to 54 ms where
 * it had taken 26 to 35, and so past the 50 ms a frame may take while an answer
 * arrives. So the line comes one frame later, and the notice of how the turn ended
 * waits for the same frame, so that the line doesn't come in above a notice that is
 * already there (ADR 0015, revision "what an answer took comes a frame after its last
 * words").
 *
 * The state is the caller's own: whoever asks this is drawn again a frame later, and
 * nothing else is. So it is asked where that costs little, by the line's own component
 * and by the chat, whose messages are memoized: never by the component that draws an
 * answer's words, which would draw them a second time. */
export function useSettled(arriving: boolean): boolean {
  const [settled, setSettled] = useState(!arriving);
  if (arriving && settled) {
    // It arrives, again or after all: a stored answer that the page reads along with.
    setSettled(false);
  }
  useEffect(() => {
    if (arriving || settled) {
      return undefined;
    }
    return afterPaint(() => {
      setSettled(true);
    });
  }, [arriving, settled]);
  return settled && !arriving;
}
