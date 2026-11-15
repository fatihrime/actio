"use client";

import { Notice, NoticeButton } from "@/components/notice";
import type { Model, Usage } from "@/lib/api/client";
import { atTime, timeLeft, useNow } from "@/lib/time";
import type { Passed, Reached } from "@/lib/usage";

const SECOND_MS = 1000;
const MINUTE_MS = 60_000;

/** Said where questions are written, while a usage limit is reached: which limit,
 * when it starts over and how long that is, and the models that cost nothing and can
 * answer until then. Once one of those answers, it says that instead. */
export function LimitReached({
  reached,
  standIn,
  offers,
  onCarryOn,
}: {
  reached: Reached;
  /** The model that costs nothing and answers meanwhile, once one was chosen. */
  standIn: Model | null;
  /** The models that cost nothing, to choose from. */
  offers: Model[];
  onCarryOn: (model: Model) => void;
}) {
  const limit = `You’ve reached your usage limit for this ${reached.window}.`;
  if (standIn !== null) {
    return (
      <Notice changing>
        <p role="status" className="font-medium">
          {standIn.title} is answering, at no cost.
        </p>
        <p>
          {limit} <Resets at={reached.resetsAt} />
        </p>
      </Notice>
    );
  }
  return (
    <Notice
      tone="warning"
      changing
      actions={
        offers.length === 0
          ? undefined
          : offers.map((model) => (
              <NoticeButton
                key={model.id}
                onClick={() => {
                  onCarryOn(model);
                }}
              >
                Use {model.title} (free)
              </NoticeButton>
            ))
      }
    >
      <p role="status" className="font-medium">
        {limit}
      </p>
      <p>
        <Resets at={reached.resetsAt} />
      </p>
    </Notice>
  );
}

/** "It resets at 2:30 PM, in 4 hours 59 minutes." The time left changes every second:
 * it is a timer, which a screen reader reads when asked and not at every change. */
function Resets({ at }: { at: Date }) {
  const now = useNow(SECOND_MS);
  return (
    <>
      It resets {atTime(at, new Date(now))},{" "}
      <span role="timer">in {timeLeft(at.getTime() - now)}</span>.
    </>
  );
}

/** Said once when a share of a usage limit has been passed: the API's warning, kept
 * until it is dismissed or the next question is sent. */
export function UsagePassed({
  passed,
  read,
  onDismiss,
}: {
  passed: Passed[];
  /** The counts as last read: they say when each window starts over. */
  read: Usage | null;
  onDismiss: () => void;
}) {
  const now = useNow(MINUTE_MS);
  if (passed.length === 0) {
    return null;
  }
  return (
    <Notice
      tone={passed.some((one) => one.percent >= 90) ? "warning" : "plain"}
      actions={<NoticeButton onClick={onDismiss}>Dismiss</NoticeButton>}
    >
      {passed.map((one) => {
        const resetsAt =
          read?.windows.find((window) => window.name === one.window)
            ?.resets_at ?? null;
        return (
          <p key={one.window}>
            You’ve used over {one.percent}% of your usage limit for this{" "}
            {one.window}.
            {resetsAt !== null &&
              ` It resets ${atTime(new Date(resetsAt), new Date(now))}.`}
          </p>
        );
      })}
    </Notice>
  );
}
