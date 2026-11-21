// The code plugin the page gives Streamdown (components/chat/markdown.tsx). It colours
// nothing itself: a worker does, which this starts at the first block of code a page
// shows (lib/chat/colouring.worker.ts). Shiki on the page's own thread kept the page
// still for as long as 1.5 s at a first block of TypeScript (ADR 0016, revision "code
// is coloured in a worker"), so nothing of shiki comes here: only Streamdown's types.
//
// Streamdown asks `highlight` again at each change of a block's code. It shows what the
// call gives back at once, or else keeps what it showed until the call's callback is
// called; and a callback does nothing once its block's code has changed again. So
// giving back nothing hands the block's text to an answer, which may come late, or
// never. Hence:
//
// - a call gives back nothing only for code that is still arriving, when the page has
//   colours for it and the worker has answered everything it was asked in that
//   language and themes: then the answer comes in a moment, and what is shown stays
//   until then, without flashing plain at each piece of code that arrives;
// - otherwise the call gives its code back at once, with the colours the page has for
//   its start, and the rest plain: the whole text is always there;
// - an answer goes to its own call alone. A block whose answers come late is coloured
//   by its next call, from the answers that came;
// - without a worker, code is shown plain, and stays readable. A worker that has left
//   too many questions unanswered is done without, as one that failed.
import type {
  CodeHighlighterPlugin,
  HighlightOptions,
  HighlightResult,
  HighlightToken,
  ThemeInput,
} from "streamdown";

import type { Answered, Asked, Colours } from "./colouring";

/** The themes Streamdown asks for: the light one and the dark. */
const THEMES: [ThemeInput, ThemeInput] = ["github-light", "github-dark"];

/** How many rows of complete blocks are remembered, in all: about fifty blocks of
 * forty lines. */
export const REMEMBERED_ROWS = 2_000;

/** How many questions may wait for the worker's answer at once. A page asks about each
 * block of code it shows before the worker can answer any, and a long conversation can
 * show a thousand; code that arrives asks a few dozen times ahead at the most. More
 * than this, and the worker has stopped answering. */
export const QUESTIONS_OUT_AT_MOST = 10_000;

type Give = (result: HighlightResult) => void;

/** A call that waits for the worker's answer. */
interface Call {
  /** Its language and themes. */
  sort: string;
  code: string;
  /** Whether its block is complete: its result is remembered. */
  complete: boolean;
  /** The result the page had for the start of the code, which the answer goes on
   * from. */
  base: Held | undefined;
  give: Give | undefined;
}

/** A result the page has, for that code. */
interface Held {
  code: string;
  result: HighlightResult;
}

/** The worker: not started yet, started, or not to be had. */
let worker: Worker | "not yet" | "gone" = "not yet";
let lastId = 0;
const waiting = new Map<number, Call>();
/** By language and themes, how many questions wait for the worker's answer. */
const out = new Map<string, number>();
/** By language and themes, the result the worker gave last. */
const latest = new Map<string, Held>();
/** The results of complete blocks, by language, themes and code, the oldest first. */
const remembered = new Map<string, HighlightResult>();
let rememberedRows = 0;
let warned = false;

/** Text as Streamdown shows code before it has colours, in the colour around it. */
function plainToken(content: string): HighlightToken {
  return {
    content,
    color: "inherit",
    bgColor: "transparent",
    htmlStyle: {},
    offset: 0,
  };
}

/** Code as Streamdown shows it before it has colours: a row a line. */
function plain(code: string): HighlightResult {
  return {
    bg: "transparent",
    fg: "inherit",
    tokens: code.split("\n").map((line) => [plainToken(line)]),
  };
}

/** The code, with the colours the page has for its start: those of the held code's
 * whole lines, and of its last line as far as that had got. What the code has gained
 * since is plain. */
function soFar(code: string, { code: had, result }: Held): HighlightResult {
  const { tokens, ...look } = result;
  const lines = code.split("\n");
  const whole = had.split("\n").length - 1;
  const gained = (lines[whole] ?? "").slice(
    had.length - had.lastIndexOf("\n") - 1,
  );
  const arriving = tokens[whole] ?? [];
  return {
    ...look,
    tokens: [
      ...tokens.slice(0, whole),
      gained === "" ? arriving : [...arriving, plainToken(gained)],
      ...lines.slice(whole + 1).map((line) => [plainToken(line)]),
    ],
  };
}

/** What the page holds for the start of this code: the worker's last result in its
 * language and themes, if this code begins with that result's code. Not a result for
 * no code at all, as when a block has just opened: it has no colours to show. */
function heldFor(sort: string, code: string): Held | undefined {
  const held = latest.get(sort);
  return held !== undefined && held.code !== "" && code.startsWith(held.code)
    ? held
    : undefined;
}

/** Say why code is shown without colours, once: not once a block. */
function warn(why: string): void {
  if (!warned) {
    warned = true;
    console.warn(why);
  }
}

/** Do without the worker from now on, and give each call that waits its code plain:
 * no block is left showing older code. */
function stop(why: string): void {
  if (typeof worker !== "string") {
    worker.terminate();
  }
  worker = "gone";
  warn(why);
  const left = [...waiting.values()];
  waiting.clear();
  out.clear();
  for (const call of left) {
    call.give?.(plain(call.code));
  }
}

function remember(key: string, result: HighlightResult): void {
  const before = remembered.get(key);
  if (before !== undefined) {
    remembered.delete(key);
    rememberedRows -= before.tokens.length;
  }
  if (result.tokens.length > REMEMBERED_ROWS) {
    return;
  }
  remembered.set(key, result);
  rememberedRows += result.tokens.length;
  for (const [oldest, { tokens }] of remembered) {
    if (rememberedRows <= REMEMBERED_ROWS) {
      break;
    }
    remembered.delete(oldest);
    rememberedRows -= tokens.length;
  }
}

/** The call of that id, which no longer waits. */
function taken(id: number): Call | undefined {
  const call = waiting.get(id);
  if (call !== undefined) {
    waiting.delete(id);
    const left = (out.get(call.sort) ?? 1) - 1;
    if (left === 0) {
      out.delete(call.sort);
    } else {
      out.set(call.sort, left);
    }
  }
  return call;
}

function answered(id: number, { keep, rows, ...look }: Colours): void {
  const call = taken(id);
  if (call === undefined) {
    return;
  }
  const result: HighlightResult = {
    ...look,
    tokens: (call.base?.result.tokens ?? []).slice(0, keep).concat(rows),
  };
  latest.set(call.sort, { code: call.code, result });
  call.give?.(result);
  if (call.complete) {
    remember(`${call.sort}\n${call.code}`, result);
  }
}

function failed(id: number, error: string): void {
  const call = taken(id);
  if (call === undefined) {
    return;
  }
  warn(`A block of code is shown without colours: ${error}`);
  call.give?.(plain(call.code));
}

/** The worker, started at the first call: a page that shows no code starts none. */
function theWorker(): Worker | undefined {
  if (worker === "not yet") {
    if (typeof Worker === "undefined") {
      stop("Code is shown without colours: this browser has no workers.");
    } else {
      try {
        const started = new Worker(
          new URL("./colouring.worker.ts", import.meta.url),
          { type: "module" },
        );
        started.onmessage = ({ data }: MessageEvent<Answered>) => {
          if ("error" in data) {
            failed(data.id, data.error);
          } else {
            answered(data.id, data.colours);
          }
        };
        started.onerror = (event: Event) => {
          // Said once, below, and not again by the browser.
          event.preventDefault();
          stop(
            event instanceof ErrorEvent
              ? `Code is shown without colours: its worker failed: ${event.message}`
              : "Code is shown without colours: its worker couldn't be loaded.",
          );
        };
        worker = started;
      } catch (error) {
        stop(
          `Code is shown without colours: its worker couldn't be started: ${String(error)}`,
        );
      }
    }
  }
  return typeof worker === "string" ? undefined : worker;
}

/** A theme's name: the worker knows themes by their names. */
function named(theme: ThemeInput): string {
  return typeof theme === "string" ? theme : (theme.name ?? "");
}

function highlight(
  { code, language, themes, isIncomplete = false }: HighlightOptions,
  callback?: Give,
): HighlightResult | null {
  const pair = [named(themes[0]), named(themes[1])] as const;
  const sort = [language, ...pair].join("\n");
  const key = `${sort}\n${code}`;
  const had = remembered.get(key);
  if (had !== undefined) {
    // The newest remembered now.
    remembered.delete(key);
    remembered.set(key, had);
    return had;
  }
  const colourer = theWorker();
  if (colourer === undefined) {
    return plain(code);
  }
  if (waiting.size >= QUESTIONS_OUT_AT_MOST) {
    stop(
      `Code is shown without colours: its worker left ${String(waiting.size)} questions unanswered.`,
    );
    return plain(code);
  }
  const base = heldFor(sort, code);
  const caughtUp = !out.has(sort);
  lastId += 1;
  const id = lastId;
  waiting.set(id, {
    sort,
    code,
    complete: !isIncomplete,
    base,
    give: callback,
  });
  out.set(sort, (out.get(sort) ?? 0) + 1);
  const asked: Asked = {
    id,
    question: { code, language, themes: pair, have: base?.code.length ?? 0 },
  };
  colourer.postMessage(asked);
  if (base === undefined) {
    return plain(code);
  }
  // Caught up, the worker answers in a moment: what is shown stays until then, where
  // plain code in its place would flicker. Behind, an answer may come late, or never.
  // And a complete block is asked about once: if that answer never came, nothing
  // would bring its last text. Its code as it was last coloured is given back as it
  // is, so nothing flickers there either.
  return caughtUp && isIncomplete ? null : soFar(code, base);
}

/** Colours code, in a worker. */
export const code: CodeHighlighterPlugin = {
  name: "shiki",
  type: "code-highlighter",
  getThemes: () => THEMES,
  highlight,
  // Streamdown never asks these two. Answering them truly would bring shiki's list of
  // languages to the page.
  supportsLanguage: () => true,
  getSupportedLanguages: () => [],
};
