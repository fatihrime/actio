"use client";

import type { UsageWindow } from "@/lib/api/client";
import { atTime, useNow } from "@/lib/time";
import { shareUsed } from "@/lib/usage";
import type { UsageLimits } from "@/lib/usage";

const NAMES: Record<UsageWindow["name"], string> = {
  session: "Session",
  week: "Week",
};

/** How often the meter looks at the clock: whether a window starts over today is the
 * only thing here that depends on it. */
const MINUTE_MS = 60_000;

/** From this share on, a window's bar says that the limit is close. */
const CLOSE = 90;

/** How much of each usage limit is used, and when each starts over.
 *
 * It shows what the API said last, and nothing the page worked out (`lib/usage.ts`).
 * A plan without limits has nothing to show. */
export function UsageMeter({ usage }: { usage: UsageLimits }) {
  const now = useNow(MINUTE_MS);
  const { read } = usage;
  if (read === null) {
    return usage.error === null ? null : (
      <p className="shrink-0 border-t border-border px-5 py-3 text-xs text-muted-foreground">
        Your usage can’t be read right now.
      </p>
    );
  }
  if (read.windows.length === 0) {
    return null;
  }
  return (
    <section
      aria-labelledby="usage-heading"
      className="shrink-0 space-y-3 border-t border-border px-5 py-3 text-xs"
    >
      <h2 className="flex items-baseline justify-between gap-2 text-muted-foreground">
        <span
          id="usage-heading"
          className="font-medium tracking-wide uppercase"
        >
          Usage
        </span>
        <span>{read.plan.title} plan</span>
      </h2>
      {read.windows.map((window) => (
        <Window key={window.name} window={window} now={now} />
      ))}
    </section>
  );
}

function Window({ window, now }: { window: UsageWindow; now: number }) {
  const { percent, label } = shareUsed(window);
  const name = `usage-${window.name}`;
  return (
    <div>
      <div className="flex items-baseline justify-between gap-2">
        <span id={name} className="font-medium">
          {NAMES[window.name]}
        </span>
        <span className="text-muted-foreground">{label} used</span>
      </div>
      <div
        role="meter"
        aria-labelledby={name}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.floor(percent)}
        aria-valuetext={`${label} used`}
        // Where a system's own colours take the place of the page's, both grounds are
        // gone: there the bar has a line around it, and is filled in the colour of
        // the words (app/globals.css).
        className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-muted forced-colors:border"
      >
        <div
          className={`h-full rounded-full forced-colors:bg-foreground ${percent >= CLOSE ? "bg-caution" : "bg-primary"}`}
          style={{ width: `${String(percent)}%` }}
        />
      </div>
      <p className="mt-1.5 text-muted-foreground">
        {window.resets_at === null
          ? // A session opens with the first question that costs something.
            "Starts with your next question"
          : `Resets ${atTime(new Date(window.resets_at), new Date(now))}`}
      </p>
    </div>
  );
}
