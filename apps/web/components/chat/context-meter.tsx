import { memo, useId } from "react";

import type { ContextFill } from "@/lib/chat/context";

/** How full the context window of the model that answers is, in this conversation: one
 * quiet line under the box where a question is written, such as "Context ▮▮▯▯ 39% full
 * · 3,214 of 8,192 tokens". The figure is the server's, and the words are
 * `contextFill`'s (`lib/chat/context.ts`).
 *
 * Its place is on the page from the box's first render, one line high, and empty until
 * there is a figure. Nothing in it wraps, so when the figure comes, changes or goes,
 * nothing else on the page moves.
 *
 * How much it says goes by the room the line itself has, not by the window's width:
 * the list of conversations stands beside the chat in some windows and not in others.
 * The share is always there; the tokens from 384 px of line (`@sm`), and the words
 * about a new chat from 576 px (`@xl`). Each is far more room than the words took
 * where they were measured, since fonts differ and a window may hold a million tokens.
 * Should the words still not fit, the bar gives way first, down to 16 px, and then
 * the words end in "…": they never reach outside the line.
 *
 * A screen reader finds a meter named "Context", which says the share and the tokens
 * once: the words beside it repeat that, and are hidden from it. It isn't told when the
 * figure changes, which is no news: the line is in no live region. */
export function ContextMeter({ fill }: { fill: ContextFill | null }) {
  return (
    <div className="@container h-4 text-xs text-muted-foreground">
      {fill !== null && <Filled {...fill} />}
    </div>
  );
}

/** The line itself, memoized on plain values: what it says changes only when a turn
 * ends, and the page around it is drawn at each piece of an answer that arrives. */
const Filled = memo(function Filled({
  percent,
  share,
  count,
  nearlyFull,
}: ContextFill) {
  const name = useId();
  const spoken = nearlyFull
    ? `${share}: ${count}. Start a new chat soon.`
    : `${share}: ${count}`;
  return (
    <div className="flex h-full items-center gap-1.5 whitespace-nowrap">
      <span id={name} className="font-medium">
        Context
      </span>
      <div
        role="meter"
        aria-labelledby={name}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.floor(percent)}
        aria-valuetext={spoken}
        // It gives way before the words do. Where a system's own colours take the
        // place of the page's, it has a line around it and is filled in the colour of
        // the words, as the usage meter's bars are (app/globals.css).
        className="h-1.5 w-24 min-w-4 shrink-[100] overflow-hidden rounded-full bg-muted forced-colors:border"
      >
        <div
          className={`h-full rounded-full forced-colors:bg-foreground ${nearlyFull ? "bg-caution" : "bg-primary"}`}
          style={{ width: `${String(percent)}%` }}
        />
      </div>
      <span
        aria-hidden="true"
        className={`min-w-0 truncate ${nearlyFull ? "text-caution-foreground" : ""}`}
      >
        {share}
        <span className="hidden @sm:inline">{` · ${count}`}</span>
        {nearlyFull && (
          <span className="hidden @xl:inline"> · start a new chat soon</span>
        )}
      </span>
    </div>
  );
});
