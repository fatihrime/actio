// What whoever has the session has used of their usage limits, and what the page knows
// about a limit that is reached.
//
// The server counts (ADR 0020). The page reads the counts with `GET /api/usage`, and
// reads them again after anything that costs credits: it never adds up what answers
// cost. That a limit is reached it learns from the API refusing a question, with a 429
// that names the window. It doesn't work that out from the counts: whether a question
// fits depends on its length, and on rules only the server has.
import { useCallback, useEffect, useState } from "react";
import useSWR from "swr";

import { ApiProblem, getUsage } from "@/lib/api/client";
import type { Usage, UsageWindow } from "@/lib/api/client";

export type WindowName = UsageWindow["name"];

const WINDOW_NAMES: readonly string[] = [
  "session",
  "week",
] satisfies WindowName[];

function isWindowName(value: unknown): value is WindowName {
  return typeof value === "string" && WINDOW_NAMES.includes(value);
}

/** How often to look at the clock while waiting for a window to start over. A timer
 * set for hours ahead comes late when the computer has slept meanwhile. */
const LOOK_AGAIN_MS = 30_000;

/** A usage limit the API refused a question for. */
export interface Reached {
  window: WindowName;
  /** When the window starts over, by this browser's clock. Questions are taken again
   * from then. */
  resetsAt: Date;
  /** The models on offer that cost nothing, by id. They answer even now. */
  free: string[];
}

/** What a refusal says about a usage limit, if it is about one.
 *
 * A 429 that names no window is no usage limit. Of a question, it is one of three: too
 * many messages in a short time, the limit on how often something is tried (ADR 0011);
 * too many answers being written at once (ADR 0023); or no model with budget left for
 * the question (ADR 0030), whose wait the chat counts down (`refusedForNow` in
 * `lib/chat/outcomes.ts`). One that names a window but no time to wait is a question
 * too large for the plan: waiting doesn't help it, so it is no limit to wait out. */
export function reachedBy(error: unknown, now: number): Reached | null {
  if (!(error instanceof ApiProblem) || error.status !== 429) {
    return null;
  }
  const { window: named, alternatives } = error.problem;
  const wait = error.retryAfter;
  if (!isWindowName(named) || wait === undefined) {
    return null;
  }
  return {
    window: named,
    // Counted from now, not read from `resets_at`: this browser's clock may be wrong
    // by minutes, and the length of the wait is the same on every clock.
    resetsAt: new Date(now + wait * 1000),
    free: Array.isArray(alternatives)
      ? alternatives.filter((id): id is string => typeof id === "string")
      : [],
  };
}

/** A share of a window that has been used up. The API says so once, in the stream of
 * the turn that passed it. */
export interface Passed {
  window: WindowName;
  /** The share that was passed: 50, 75 or 90. More than that may be used by now. */
  percent: number;
}

/** A usage warning from a turn's stream, if `data` is one. */
export function passedIn(data: unknown): Passed | null {
  if (
    typeof data !== "object" ||
    data === null ||
    !("window" in data) ||
    !("percent" in data)
  ) {
    return null;
  }
  const { window: named, percent } = data;
  return isWindowName(named) && typeof percent === "number"
    ? { window: named, percent }
    : null;
}

/** How full a window is. What is held for answers that are being written isn't
 * counted: it comes back when they end, for all that they didn't use. */
export function shareUsed(window: UsageWindow): Share {
  return shareOf(window.used, window.limit);
}

/** A share of something that fills up. */
export interface Share {
  /** For a bar: from 0 to 100. */
  percent: number;
  /** For reading, in whole percent: never 0% once something is used, or 100% before
   * everything is. */
  label: string;
}

/** How much of `whole` is `part`, as a share: none over 100%. A whole of nothing is
 * full. */
export function shareOf(part: number, whole: number): Share {
  const percent =
    whole <= 0 ? 100 : Math.min(100, Math.max(0, (100 * part) / whole));
  const label =
    percent > 0 && percent < 1 ? "under 1%" : `${String(Math.floor(percent))}%`;
  return { percent, label };
}

export interface UsageLimits {
  /** The plan and its windows, as the API last said. Nothing until it has. */
  read: Usage | null;
  /** What went wrong the last time they were read, if anything did. */
  error: unknown;
  /** Read them again: something has happened that costs credits. */
  refresh: () => void;
  /** The limit that is reached, until its window starts over. */
  reached: Reached | null;
  /** The API refused a question with `error`. Says whether that was a usage limit,
   * which is then held as `reached`. */
  refused: (error: unknown) => boolean;
  /** The model that costs nothing and answers while the limit is reached, by id, once
   * the person has chosen one there, and until they choose a model in the picker. */
  standIn: string | null;
  /** Carry on with a model that costs nothing, until the limit starts over. */
  carryOnWith: (model: string) => void;
  /** The person chose a model in the picker: the one that stood in no longer answers,
   * and the limit stays reached (ADR 0032). */
  endStandIn: () => void;
  /** The shares of windows the API said were passed, and nobody has dismissed: for
   * each window the one it said last. */
  passed: Passed[];
  /** A turn's stream carried `data`, which may be a usage warning. */
  pass: (data: unknown) => void;
  /** Put the warnings away. */
  dismiss: () => void;
}

/** What a session was told about its limits, beside the counts. */
interface Told {
  reached: Reached | null;
  standIn: string | null;
  passed: Passed[];
}

const NOTHING: Told = { reached: null, standIn: null, passed: [] };

/** The usage limits of whoever has the session. `holder` is what the session's data
 * is held under (`Session.holder`): nothing read or said for one session is shown in
 * the next. Nothing is read while there is no session. */
export function useUsage(holder: string | null): UsageLimits {
  const { data, error, mutate } = useSWR<Usage, unknown>(
    holder === null ? null : (["usage", holder] as const),
    getUsage,
    // A failure is said under the meter, quietly. The counts are read again after
    // every turn and whenever the window is returned to, which is retry enough.
    { shouldRetryOnError: false },
  );
  const [said, setSaid] = useState<{ holder: string; told: Told } | null>(null);
  const told = said !== null && said.holder === holder ? said.told : NOTHING;

  const tell = useCallback(
    (change: (told: Told) => Told) => {
      if (holder !== null) {
        setSaid((before) => ({
          holder,
          told: change(before?.holder === holder ? before.told : NOTHING),
        }));
      }
    },
    [holder],
  );

  const refresh = useCallback(() => {
    void mutate();
  }, [mutate]);

  const refused = useCallback(
    (refusal: unknown) => {
      const reached = reachedBy(refusal, Date.now());
      if (reached === null) {
        return false;
      }
      // What was passed on the way here, the limit itself says now.
      tell(() => ({ reached, standIn: null, passed: [] }));
      return true;
    },
    [tell],
  );

  const carryOnWith = useCallback(
    (model: string) => {
      tell((before) => ({ ...before, standIn: model }));
    },
    [tell],
  );

  const endStandIn = useCallback(() => {
    tell((before) => ({ ...before, standIn: null }));
  }, [tell]);

  const pass = useCallback(
    (data: unknown) => {
      const passed = passedIn(data);
      if (passed !== null) {
        // A turn that passes two shares of a window says the smaller one first.
        tell((before) => ({
          ...before,
          passed: [
            ...before.passed.filter(
              (earlier) => earlier.window !== passed.window,
            ),
            passed,
          ],
        }));
      }
    },
    [tell],
  );

  const dismiss = useCallback(() => {
    tell((before) => ({ ...before, passed: [] }));
  }, [tell]);

  // When a window starts over, the counts are read again, and a limit that was
  // reached in it is over.
  const reachedAt = told.reached?.resetsAt.getTime() ?? null;
  useEffect(() => {
    const start = Date.now();
    let ahead = [
      reachedAt,
      ...(data?.windows ?? []).map((window) =>
        window.resets_at === null ? null : Date.parse(window.resets_at),
      ),
    ].filter((moment): moment is number => moment !== null && moment > start);
    let timer: ReturnType<typeof setTimeout> | undefined;

    function look() {
      const now = Date.now();
      const due = ahead.filter((moment) => moment <= now);
      ahead = ahead.filter((moment) => moment > now);
      if (due.length > 0) {
        if (reachedAt !== null && due.includes(reachedAt)) {
          tell(() => NOTHING);
        }
        void mutate();
      }
      waitForNext(now);
    }

    function waitForNext(now: number) {
      timer =
        ahead.length === 0
          ? undefined
          : setTimeout(look, Math.min(Math.min(...ahead) - now, LOOK_AGAIN_MS));
    }

    waitForNext(start);
    return () => {
      clearTimeout(timer);
    };
  }, [reachedAt, data, tell, mutate]);

  return {
    read: data ?? null,
    error: error ?? null,
    refresh,
    reached: told.reached,
    refused,
    standIn: told.standIn,
    carryOnWith,
    endStandIn,
    passed: told.passed,
    pass,
    dismiss,
  };
}
