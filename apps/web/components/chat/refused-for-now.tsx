"use client";

import { useEffect, useEffectEvent, useRef } from "react";

import { Notice, NoticeButton } from "@/components/notice";
import { timeLeft, useNow } from "@/lib/time";

const SECOND_MS = 1000;

/** Said where questions are written when a question was refused for now: no model that
 * could answer it has budget left for it, by Actio's count of what its models'
 * providers take (`refusedForNow` in `lib/chat/outcomes.ts`). The time left counts down,
 * and the notice goes by itself when the wait is over. Nothing is sent then, or kept
 * from being sent before: the question waits in the box for its writer, and a shorter
 * one may be taken sooner.
 *
 * The clock is read here, every second, and not by the conversation around the notice,
 * which would be drawn again with it. It is looked at, not counted: after a computer
 * slept, the next look is right.
 *
 * A screen reader says the first sentence once, as a status. The time left is a timer
 * beside it, which it reads when asked for, and not at every second (ADR 0021). The
 * time of day isn't said: the wait is an estimate for one request, mostly seconds or
 * minutes. */
export function RefusedForNow({
  until,
  onDismiss,
  onOver,
}: {
  /** When the wait is over, by this browser's clock. */
  until: number;
  onDismiss: () => void;
  /** The wait is over. `hadKeyboard` says whether the keyboard was on the notice's
   * button, which goes with it. */
  onOver: (hadKeyboard: boolean) => void;
}) {
  const now = useNow(SECOND_MS);
  const over = now >= until;
  // `NoticeButton` takes no ref: whether the keyboard is on it is asked of what holds
  // the notice, in which it is the one thing that can have the keyboard.
  const here = useRef<HTMLDivElement>(null);
  const end = useEffectEvent(() => {
    onOver(here.current?.contains(document.activeElement) ?? false);
  });
  useEffect(() => {
    if (over) {
      end();
    }
  }, [over]);
  return (
    <div ref={here}>
      <Notice
        tone="warning"
        changing
        actions={<NoticeButton onClick={onDismiss}>Dismiss</NoticeButton>}
      >
        <p role="status" className="font-medium">
          No model can take this message right now.
        </p>
        <p>
          This installation has reached the limits of its models’ providers. Try
          again <span role="timer">in {timeLeft(until - now)}</span>.
        </p>
      </Notice>
    </div>
  );
}
