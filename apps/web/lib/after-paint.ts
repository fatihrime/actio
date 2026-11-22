// What waits until the browser has painted what is on the page: a frame's callback runs
// before that frame is painted, and a task begun from it runs after.

/** Call `then` once the browser has painted its next frame: in a task begun from that
 * frame's callback (`requestAnimationFrame`). Hands back a function that calls it
 * off, before the frame or after it.
 *
 * A page that isn't shown, as in a tab in the background, paints no frames: `then`
 * waits until it is shown. */
export function afterPaint(then: () => void): () => void {
  let task: ReturnType<typeof setTimeout> | undefined;
  const frame = requestAnimationFrame(() => {
    task = setTimeout(then, 0);
  });
  return () => {
    cancelAnimationFrame(frame);
    clearTimeout(task);
  };
}
