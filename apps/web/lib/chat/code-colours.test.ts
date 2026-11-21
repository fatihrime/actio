// The code plugin the page gives Streamdown (code-colours.ts), with a stand-in for the
// worker that colours code, which each test answers by hand. The plugin holds the
// worker and what it remembers for as long as the page is open: so each test loads it
// anew, as a page that has just been opened.
import type {
  CodeHighlighterPlugin,
  HighlightResult,
  HighlightToken,
} from "streamdown";
import { beforeEach, describe, expect, test, vi } from "vitest";

import type { Answered, Asked } from "./colouring";

/** The workers started, the first first. */
let started: StandIn[] = [];

/** Stands in for the worker that colours code. */
class StandIn {
  onmessage: ((event: MessageEvent<Answered>) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  /** What it was asked, the first first. */
  readonly asked: Asked[] = [];
  terminated = false;

  constructor(
    readonly url: URL,
    readonly options: WorkerOptions,
  ) {
    started.push(this);
  }

  postMessage(message: Asked): void {
    this.asked.push(message);
  }

  terminate(): void {
    this.terminated = true;
  }

  /** What it was asked about this code last. */
  question(code: string): Asked {
    const asked = this.asked.findLast((each) => each.question.code === code);
    if (asked === undefined) {
      throw new Error(`The worker wasn't asked about "${code}".`);
    }
    return asked;
  }

  /** Answer what it was asked about that code, as the worker does: each line in that
   * colour, without the rows of the whole lines the page said it has. */
  answer(code: string, colour: string): void {
    const { id, question } = this.question(code);
    const keep = code.slice(0, question.have).split("\n").length - 1;
    this.tell({
      id,
      colours: { ...LOOK, keep, rows: rows(code, colour).slice(keep) },
    });
  }

  /** Answer with an error, as when a language couldn't be loaded. */
  refuse(code: string): void {
    this.tell({
      id: this.question(code).id,
      error: "The language couldn't be loaded.",
    });
  }

  /** Fail, as a worker whose script couldn't be loaded does. */
  fail(): void {
    this.onerror?.(new Event("error"));
  }

  private tell(answer: Answered): void {
    this.onmessage?.(new MessageEvent("message", { data: answer }));
  }
}

let code: CodeHighlighterPlugin;
let REMEMBERED_ROWS: number;
let QUESTIONS_OUT_AT_MOST: number;

beforeEach(async () => {
  started = [];
  vi.stubGlobal("Worker", StandIn);
  vi.resetModules();
  ({ code, REMEMBERED_ROWS, QUESTIONS_OUT_AT_MOST } =
    await import("./code-colours"));
});

/** The worker started, which there is to be one of. */
function theWorker(): StandIn {
  expect(started).toHaveLength(1);
  return started[0] as StandIn;
}

/** Ask the plugin about a block, as Streamdown does at each change of its code. */
function highlight(
  text: string,
  { language = "typescript", arriving = false } = {},
) {
  const give = vi.fn<(result: HighlightResult) => void>();
  const shown = code.highlight(
    { code: text, language, themes: code.getThemes(), isIncomplete: arriving },
    give,
  );
  return { text, shown, give };
}

/** Text as Streamdown itself shows code before it has colours. */
function plainToken(content: string): HighlightToken {
  return {
    content,
    color: "inherit",
    bgColor: "transparent",
    htmlStyle: {},
    offset: 0,
  };
}

/** Code as Streamdown itself shows it before it has colours. */
function plain(text: string): HighlightResult {
  return {
    bg: "transparent",
    fg: "inherit",
    tokens: text.split("\n").map((line) => [plainToken(line)]),
  };
}

/** Text in a colour that says which answer it came from. */
function inColour(content: string, colour: string): HighlightToken {
  return { content, offset: 0, htmlStyle: { color: colour } };
}

/** Rows of one token a line, in that colour. */
function rows(text: string, colour: string): HighlightToken[][] {
  return text.split("\n").map((line) => [inColour(line, colour)]);
}

const LOOK = { fg: "#24292e", bg: "#fff" };

/** The text a result shows, a line a row. */
function textOf(result: HighlightResult): string {
  return result.tokens
    .map((row) => row.map((token) => token.content).join(""))
    .join("\n");
}

/** Whether each piece of a result's text has colours. */
function allColoured(result: HighlightResult | undefined): boolean {
  return (
    result !== undefined &&
    result.tokens.every((row) =>
      row.every((token) => token.htmlStyle?.color !== undefined),
    )
  );
}

test("the themes are the two Streamdown asks for, in the same array each time", () => {
  // Streamdown asks about every block again when the themes it is given change.
  expect(code.getThemes()).toEqual(["github-light", "github-dark"]);
  expect(code.getThemes()).toBe(code.getThemes());
});

describe("without a worker", () => {
  test("in a browser without workers, code is given back plain at once", () => {
    vi.stubGlobal("Worker", undefined);
    const warned = vi.spyOn(console, "warn").mockReturnValue();

    const arriving = highlight("const a = 1;\nconst", { arriving: true });
    const complete = highlight("body {\n}", { language: "css" });

    // Exactly as Streamdown shows code before it has colours.
    expect(arriving.shown).toEqual({
      bg: "transparent",
      fg: "inherit",
      tokens: [
        [
          {
            content: "const a = 1;",
            color: "inherit",
            bgColor: "transparent",
            htmlStyle: {},
            offset: 0,
          },
        ],
        [
          {
            content: "const",
            color: "inherit",
            bgColor: "transparent",
            htmlStyle: {},
            offset: 0,
          },
        ],
      ],
    });
    expect(complete.shown).toEqual(plain("body {\n}"));
    expect(warned).toHaveBeenCalledOnce();
  });

  test("a worker that can't be started: code is given back plain at once", () => {
    // As a browser does whose page may start no worker.
    vi.stubGlobal("Worker", function Refused() {
      throw new DOMException("Not allowed.", "SecurityError");
    });
    const warned = vi.spyOn(console, "warn").mockReturnValue();

    expect(highlight("const a", { arriving: true }).shown).toEqual(
      plain("const a"),
    );
    expect(highlight("let b").shown).toEqual(plain("let b"));
    expect(warned).toHaveBeenCalledOnce();
  });

  test("a worker that fails: what waited is given its code plain, and what follows at once", () => {
    const warned = vi.spyOn(console, "warn").mockReturnValue();
    highlight("const a", { arriving: true });
    theWorker().answer("const a", "#111");
    // One that has colours, and one that has none yet.
    const withColours = highlight("const a = 1", { arriving: true });
    const without = highlight("body {", { language: "css", arriving: true });
    expect(withColours.shown).toBeNull();

    theWorker().fail();

    expect(withColours.give).toHaveBeenCalledExactlyOnceWith(
      plain("const a = 1"),
    );
    expect(without.give).toHaveBeenCalledExactlyOnceWith(plain("body {"));
    expect(theWorker().terminated).toBe(true);
    const later = highlight("const a = 1;", { arriving: true });
    expect(later.shown).toEqual(plain("const a = 1;"));
    expect(theWorker().asked).toHaveLength(3);
    expect(warned).toHaveBeenCalledOnce();
  });

  test("an answer that is an error: that block is given its code plain", () => {
    const warned = vi.spyOn(console, "warn").mockReturnValue();
    const failing = highlight("x := 1", { language: "velora" });
    const other = highlight("body {", { language: "css" });

    theWorker().refuse("x := 1");

    expect(failing.give).toHaveBeenCalledExactlyOnceWith(plain("x := 1"));
    expect(other.give).not.toHaveBeenCalled();
    // The worker still colours the rest.
    theWorker().answer("body {", "#111");
    expect(other.give).toHaveBeenCalledExactlyOnceWith({
      ...LOOK,
      tokens: rows("body {", "#111"),
    });
    const again = highlight("y := 2", { language: "velora" });
    expect(theWorker().asked).toHaveLength(3);
    theWorker().refuse("y := 2");
    expect(again.give).toHaveBeenCalledExactlyOnceWith(plain("y := 2"));
    expect(theWorker().terminated).toBe(false);
    expect(warned).toHaveBeenCalledOnce();
  });

  test("a worker that leaves too many questions unanswered is done without", () => {
    const warned = vi.spyOn(console, "warn").mockReturnValue();
    const waited = Array.from({ length: QUESTIONS_OUT_AT_MOST }, (_, at) =>
      highlight(`const a${String(at)}`, { arriving: true }),
    );
    expect(theWorker().terminated).toBe(false);

    const one = highlight("const b", { arriving: true });

    expect(one.shown).toEqual(plain("const b"));
    expect(theWorker().terminated).toBe(true);
    expect(theWorker().asked).toHaveLength(QUESTIONS_OUT_AT_MOST);
    expect(
      waited.every(
        ({ give, text }) =>
          give.mock.calls.length === 1 &&
          textOf(give.mock.calls[0]?.[0] ?? plain("")) === text,
      ),
    ).toBe(true);
    expect(waited.at(-1)?.give).toHaveBeenCalledWith(
      plain(`const a${String(QUESTIONS_OUT_AT_MOST - 1)}`),
    );
    expect(warned).toHaveBeenCalledOnce();
  });
});

describe("a page with much code", () => {
  test("two thousand blocks asked about in one go leave the worker at work", () => {
    // A long conversation opened by its address asks about each of its blocks before
    // the worker can answer any. A bound below that would take the colours from the
    // whole page: the number is held here, not only its name.
    const warned = vi.spyOn(console, "warn").mockReturnValue();

    const blocks = Array.from({ length: 2_000 }, (_, at) =>
      highlight(`const part${String(at)} = ${String(at)};`),
    );

    expect(theWorker().terminated).toBe(false);
    expect(theWorker().asked).toHaveLength(2_000);
    expect(warned).not.toHaveBeenCalled();
    theWorker().answer("const part1999 = 1999;", "#111");
    expect(allColoured(blocks.at(-1)?.give.mock.lastCall?.[0])).toBe(true);
  });
});

describe("with a worker", () => {
  test("the first call gives its code back plain, starts the worker and asks it", () => {
    // A page that shows no code starts no worker.
    expect(started).toEqual([]);

    const first = highlight("const a", { arriving: true });

    expect(first.shown).toEqual(plain("const a"));
    const worker = theWorker();
    expect(worker.url.pathname).toMatch(/\/lib\/chat\/colouring\.worker\.ts$/);
    expect(worker.options).toEqual({ type: "module" });
    expect(worker.asked).toEqual([
      {
        id: expect.any(Number) as unknown,
        question: {
          code: "const a",
          language: "typescript",
          themes: ["github-light", "github-dark"],
          have: 0,
        },
      },
    ]);

    worker.answer("const a", "#111");

    expect(first.give).toHaveBeenCalledExactlyOnceWith({
      ...LOOK,
      tokens: rows("const a", "#111"),
    });
    highlight("body {", { language: "css" });
    expect(theWorker().asked).toHaveLength(2);
  });

  test("a page's first block shows the whole of its code at every call, however late the answers", () => {
    const pieces = [
      "const a = 1;\nconst",
      "const a = 1;\nconst b",
      "const a = 1;\nconst b = 2;",
      "const a = 1;\nconst b = 2;\nconst",
      "const a = 1;\nconst b = 2;\nconst c",
      "const a = 1;\nconst b = 2;\nconst c = 3;\nconst d",
      "const a = 1;\nconst b = 2;\nconst c = 3;\nconst d = 4;",
    ] as const;
    const [, , , , , sixth, seventh] = pieces;

    // Five calls before the worker has answered any: each is given its code plain.
    const calls = pieces
      .slice(0, 5)
      .map((text) => highlight(text, { arriving: true }));
    for (const call of calls) {
      expect(call.shown).toEqual(plain(call.text));
    }
    theWorker().answer(pieces[0], "#111");

    // Four answers are still out: the code is given at once, with the colours of the
    // one answer that came, and plain where it has grown since.
    const behind = highlight(sixth, { arriving: true });
    expect(behind.shown).toEqual({
      ...LOOK,
      tokens: [
        [inColour("const a = 1;", "#111")],
        [inColour("const", "#111"), plainToken(" b = 2;")],
        [plainToken("const c = 3;")],
        [plainToken("const d")],
      ],
    });
    calls.push(behind);

    // The rest come in a burst. Then the worker is caught up: what is shown stays
    // until the answer, which takes a moment.
    for (const text of pieces.slice(1, 6)) {
      theWorker().answer(text, "#222");
    }
    const caughtUp = highlight(seventh, { arriving: true });
    expect(caughtUp.shown).toBeNull();
    theWorker().answer(seventh, "#333");
    calls.push(caughtUp);

    // Whatever a call was given, at once or later, holds the whole of its code.
    for (const { text, shown, give } of calls) {
      for (const result of [shown, ...give.mock.calls.map(([each]) => each)]) {
        if (result !== null) {
          expect(textOf(result)).toBe(text);
        }
      }
    }
    expect(allColoured(caughtUp.give.mock.lastCall?.[0])).toBe(true);
  });

  test("a block with colours, and nothing waiting in its language: what is shown stays until the answer", () => {
    highlight("const a = 1;\nconst b", { arriving: true });
    theWorker().answer("const a = 1;\nconst b", "#111");

    const more = highlight("const a = 1;\nconst b = 2;\nconst c", {
      arriving: true,
    });

    expect(more.shown).toBeNull();
    expect(theWorker().asked[1]?.question.have).toBe(
      "const a = 1;\nconst b".length,
    );
    theWorker().answer("const a = 1;\nconst b = 2;\nconst c", "#222");
    expect(more.give).toHaveBeenCalledExactlyOnceWith({
      ...LOOK,
      tokens: [
        ...rows("const a = 1;", "#111"),
        ...rows("const b = 2;\nconst c", "#222"),
      ],
    });
  });

  test("a block with colours whose last question is still out: its code at once, with the colours there are", () => {
    highlight("const a = 1;\nconst b", { arriving: true });
    theWorker().answer("const a = 1;\nconst b", "#111");
    highlight("const a = 1;\nconst b = 2", { arriving: true });

    const behind = highlight("const a = 1;\nconst b = 2;\nconst c", {
      arriving: true,
    });

    expect(behind.shown).toEqual({
      ...LOOK,
      tokens: [
        [inColour("const a = 1;", "#111")],
        [inColour("const b", "#111"), plainToken(" = 2;")],
        [plainToken("const c")],
      ],
    });
  });

  test("a worker that stops answering leaves no text behind past one call", () => {
    highlight("const a = 1;", { arriving: true });
    theWorker().answer("const a = 1;", "#111");

    // The page can't tell yet that this question will never be answered.
    expect(
      highlight("const a = 1;\nconst b", { arriving: true }).shown,
    ).toBeNull();

    // Every call after it is given its whole code at once, the complete one too.
    for (const { text, shown } of [
      highlight("const a = 1;\nconst b = 2;", { arriving: true }),
      highlight("const a = 1;\nconst b = 2;\nconst c", { arriving: true }),
      highlight("const a = 1;\nconst b = 2;\nconst c = 3;"),
    ]) {
      expect(shown).not.toBeNull();
      expect(textOf(shown ?? plain(""))).toBe(text);
    }
    expect(theWorker().asked).toHaveLength(5);
  });

  test("an answer that is an error counts as an answer: the worker is caught up again", () => {
    const warned = vi.spyOn(console, "warn").mockReturnValue();
    highlight("const a = 1;\nconst b", { arriving: true });
    theWorker().answer("const a = 1;\nconst b", "#111");
    highlight("const a = 1;\nconst b = 2;", { arriving: true });

    theWorker().refuse("const a = 1;\nconst b = 2;");

    // Nothing waits in the language any more: the next piece is asked about as ever.
    expect(
      highlight("const a = 1;\nconst b = 2;\nconst c", { arriving: true })
        .shown,
    ).toBeNull();
    expect(warned).toHaveBeenCalledOnce();
  });

  test("a complete block never waits for its text, also when the worker is caught up", () => {
    // A block is asked about once more when it is complete, and never again: were that
    // call given nothing, and its answer never came, its last text would stay away.
    highlight("const a = 1;\nconst b", { arriving: true });
    theWorker().answer("const a = 1;\nconst b", "#111");

    const complete = highlight("const a = 1;\nconst b = 2;");

    expect(complete.shown).toEqual({
      ...LOOK,
      tokens: [
        ...rows("const a = 1;", "#111"),
        [inColour("const b", "#111"), plainToken(" = 2;")],
      ],
    });
    expect(theWorker().asked).toHaveLength(2);
    theWorker().answer("const a = 1;\nconst b = 2;", "#222");
    expect(allColoured(complete.give.mock.lastCall?.[0])).toBe(true);
  });

  test("a block that completes with the code it was last coloured with is given those colours as they are", () => {
    // The usual end of a block: its closing fence arrives, and its code is what it was.
    highlight("const a = 1;\nconst b = 2;", { arriving: true });
    theWorker().answer("const a = 1;\nconst b = 2;", "#111");

    const complete = highlight("const a = 1;\nconst b = 2;");

    expect(complete.shown).toEqual({
      ...LOOK,
      tokens: rows("const a = 1;\nconst b = 2;", "#111"),
    });
  });

  test("a block that has just opened has no colours to keep", () => {
    highlight("", { arriving: true });
    theWorker().answer("", "#111");

    const next = highlight("const", { arriving: true });

    expect(next.shown).toEqual(plain("const"));
  });

  test("a complete block's answer goes to that block alone, even when another block begins with its code", () => {
    // A model that shows a module twice, the second time longer.
    const shorter = highlight("import a");
    const longer = highlight("import a\nimport b", { arriving: true });

    theWorker().answer("import a", "#111");

    expect(shorter.give).toHaveBeenCalledOnce();
    expect(longer.give).not.toHaveBeenCalled();
  });
});

describe("what is remembered", () => {
  test("a complete block asked about again is answered at once, without the worker", () => {
    highlight("const a = 1;");
    theWorker().answer("const a = 1;", "#111");

    const again = highlight("const a = 1;");

    expect(again.shown).toEqual({
      ...LOOK,
      tokens: rows("const a = 1;", "#111"),
    });
    expect(theWorker().asked).toHaveLength(1);
  });

  test("a block that was arriving is not remembered that way", () => {
    highlight("const a = 1;", { arriving: true });
    theWorker().answer("const a = 1;", "#111");

    highlight("const a = 1;");

    // Asked about again, as a complete block now: what it is given at once is what
    // the page holds for the arriving code, not something remembered.
    expect(theWorker().asked).toHaveLength(2);
    expect(theWorker().asked[1]?.question.have).toBe("const a = 1;".length);
  });

  test("so many rows are remembered, and no more: the oldest go first", () => {
    /** A complete block of so many lines, each its own. */
    function block(word: string, lines: number): string {
      return Array.from(
        { length: lines },
        (_, at) => `${word} ${String(at)}`,
      ).join("\n");
    }
    const half = REMEMBERED_ROWS / 2;
    const [first, second, third] = ["a", "b", "c"].map((word) =>
      block(word, half),
    ) as [string, string, string];
    for (const text of [first, second, third]) {
      highlight(text);
      theWorker().answer(text, "#111");
    }
    const asked = theWorker().asked.length;

    expect(highlight(third).shown?.tokens).toHaveLength(half);
    expect(highlight(second).shown?.tokens).toHaveLength(half);
    expect(theWorker().asked).toHaveLength(asked);
    expect(highlight(first).shown).toEqual(plain(first));
    expect(theWorker().asked).toHaveLength(asked + 1);
  });
});
