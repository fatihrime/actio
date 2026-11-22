// Waiting until the browser has painted its next frame (after-paint.ts), with the
// frames in the test's hands: a frame comes when the test says so.
import { describe, expect, it, vi } from "vitest";

import { holdFrames } from "@/tests/frames";

import { afterPaint } from "./after-paint";

/** Let the tasks begun by now run: this one comes after them. */
async function theTasksBegun(): Promise<void> {
  await new Promise<void>((resolve) => {
    setTimeout(resolve, 0);
  });
}

describe("after the next paint", () => {
  it("calls nothing before the next frame, nor in it, and once in the task after it", async () => {
    const nextFrame = holdFrames();
    const then = vi.fn();

    afterPaint(then);
    await theTasksBegun();
    expect(then).not.toHaveBeenCalled();

    // A frame's callbacks run before it is painted.
    nextFrame();
    expect(then).not.toHaveBeenCalled();

    await theTasksBegun();
    expect(then).toHaveBeenCalledOnce();

    // One frame, not each.
    nextFrame();
    await theTasksBegun();
    expect(then).toHaveBeenCalledOnce();
  });

  it("calls nothing once it is called off before the frame", async () => {
    const nextFrame = holdFrames();
    const then = vi.fn();

    afterPaint(then)();
    nextFrame();
    await theTasksBegun();

    expect(then).not.toHaveBeenCalled();
  });

  it("calls nothing once it is called off between the frame and the task after it", async () => {
    const nextFrame = holdFrames();
    const then = vi.fn();

    const callOff = afterPaint(then);
    nextFrame();
    callOff();
    await theTasksBegun();

    expect(then).not.toHaveBeenCalled();
  });
});
