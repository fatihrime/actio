// @vitest-environment node
//
// The worker's side of colouring code (colouring.ts), with the real shiki and its
// WebAssembly engine: the rows a block of code is drawn with. Shiki is pinned to an
// exact version, so an upgrade that changes these rows fails here first.
//
// In Node and not in the simulated page: shiki unpacks its WebAssembly through a
// `Blob`'s stream, which the simulated page's `Blob` doesn't have. A worker has one.
import { codeToTokens } from "shiki";
import type * as Shiki from "shiki";
import type { HighlightToken } from "streamdown";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import type * as Colouring from "./colouring";

const THEMES = ["github-light", "github-dark"] as const;

let colour: typeof Colouring.colour;

beforeEach(async () => {
  // The worker keeps what it worked out for code it was asked about. Each test starts
  // from a worker that has been asked nothing.
  vi.resetModules();
  ({ colour } = await import("./colouring"));
});

function ask(code: string, { language = "typescript", have = 0 } = {}) {
  return colour({ code, language, themes: THEMES, have });
}

/** What shiki gives for the whole code at once, as the page draws it. */
async function atOnce(code: string): Promise<HighlightToken[][]> {
  const { tokens } = await codeToTokens(code, {
    lang: "typescript",
    themes: { light: THEMES[0], dark: THEMES[1] },
  });
  return tokens;
}

/** The text of each row. */
function texts(rows: HighlightToken[][]): string[] {
  return rows.map((row) => row.map((token) => token.content).join(""));
}

/** The token that holds exactly this text. */
function token(rows: HighlightToken[][], content: string): HighlightToken {
  const found = rows.flat().find((each) => each.content === content);
  if (found === undefined) {
    throw new Error(`No token holds "${content}".`);
  }
  return found;
}

describe("colours", () => {
  test("TypeScript is coloured, for the light theme and for the dark", async () => {
    const { rows } = await ask("const wheels = 2; // a bike");

    const keyword = token(rows, "const");
    const comment = token(rows, "// a bike");
    // Streamdown draws `color` in the light theme and `--shiki-dark` in the dark.
    expect(keyword.htmlStyle).toEqual({
      color: expect.stringMatching(/^#[0-9A-F]{6}$/i) as unknown,
      "--shiki-dark": expect.stringMatching(/^#[0-9A-F]{6}$/i) as unknown,
    });
    expect(comment.htmlStyle?.color).not.toBe(keyword.htmlStyle?.color);
    expect(comment.htmlStyle?.["--shiki-dark"]).not.toBe(
      keyword.htmlStyle?.["--shiki-dark"],
    );
  });

  test("a language is found by its alias as by its name", async () => {
    const code = "const wheels = 2; // a bike";
    const named = await ask(code, { language: "typescript" });

    expect(await ask(code, { language: "ts" })).toEqual(named);
    expect(await ask(code, { language: "TypeScript" })).toEqual(named);
  });

  test("a language shiki doesn't know is plain text, and no error", async () => {
    const { rows } = await ask("const wheels = 2;\n// a bike", {
      language: "velora-script",
    });

    expect(rows).toEqual([
      [{ content: "const wheels = 2;", offset: 0, htmlStyle: {} }],
      [{ content: "// a bike", offset: 18, htmlStyle: {} }],
    ]);
  });
});

describe("code that is arriving", () => {
  // A comment over several lines, and a string over several lines: how a line is
  // coloured depends on the lines before it.
  const CODE = [
    "/**",
    " * A bike's parts,",
    " * and what they cost.",
    " */",
    "const parts = [`saddle",
    "",
    "chain`, 'pedals'];",
    "",
    "function total(prices: number[]): number {",
    "  return prices.reduce((sum, price) => sum + price, 0); // all of them",
    "}",
  ].join("\n");

  /** The same, with lines that end in "\r\n", which shiki takes for a line break too. */
  const CRLF = CODE.replaceAll("\n", "\r\n");

  /** Where each line break is: the code up to it, and with it. */
  function atBreaks(code: string): number[] {
    return [...code.matchAll(/\r?\n/g)].flatMap(({ index, 0: lineBreak }) => [
      index,
      index + lineBreak.length,
    ]);
  }

  /** The middle of each line. */
  const IN_LINES = [...CODE.matchAll(/^.*$/gm)].map(
    ({ index, 0: line }) => index + Math.floor(line.length / 2),
  );

  test.each([
    ["at every line break", CODE, atBreaks(CODE)],
    ["in the middle of every line", CODE, IN_LINES],
    [
      'at every line break, of lines that end in "\\r\\n"',
      CRLF,
      atBreaks(CRLF),
    ],
  ])(
    "asked about piece by piece, cut %s, it is coloured as the whole is",
    async (_, code, cuts) => {
      // As the page does: it says how much it has rows for, keeps as many rows as the
      // answer says, and puts the rows of the answer after them.
      let had = "";
      let rows: HighlightToken[][] = [];
      for (const cut of [...cuts, code.length]) {
        const piece = code.slice(0, cut);
        const answer = await ask(piece, { have: had.length });
        rows = rows.slice(0, answer.keep).concat(answer.rows);

        expect(rows).toEqual(await atOnce(piece));
        had = piece;
      }
    },
  );

  test("the answer leaves out the rows of the whole lines the page has", async () => {
    const first = "const a = 1;\nconst b = 2;\nconst c";
    const answer = await ask(first);
    expect(answer.keep).toBe(0);
    expect(texts(answer.rows)).toEqual([
      "const a = 1;",
      "const b = 2;",
      "const c",
    ]);

    const more = await ask(
      "const a = 1;\nconst b = 2;\nconst c = 3;\nconst d",
      {
        have: first.length,
      },
    );

    expect(more.keep).toBe(2);
    expect(texts(more.rows)).toEqual(["const c = 3;", "const d"]);
  });

  test("nothing is left out of the answer when the page has nothing", async () => {
    await ask("const a = 1;\nconst b = 2;");

    // This code begins as the last one did, but not with all of it: so the page has
    // no rows for it, though the worker kept those of its first line.
    const other = await ask("const a = 1;\nlet b = 2;", { have: 0 });

    expect(other.keep).toBe(0);
    expect(texts(other.rows)).toEqual(["const a = 1;", "let b = 2;"]);
  });
});

type HighlighterOptions = Parameters<typeof Shiki.createHighlighter>[0];

/** What the worker's module asked of shiki, which the tests below wrap. */
const told = {
  /** The options of each highlighter it made. */
  made: [] as HighlighterOptions[],
  /** Each WebAssembly engine it made. */
  engines: [] as unknown[],
  /** How many characters of code it had coloured, in all. */
  characters: 0,
  /** How many of the next highlighters fail to be made, as when the engine couldn't
   * be fetched. */
  makingFails: 0,
  /** How many of the next loads of a language fail, as when its grammar couldn't be
   * fetched. */
  loadingFails: 0,
  /** How many times a language was asked for. */
  loads: 0,
};

describe("what is asked of shiki", () => {
  beforeEach(async () => {
    Object.assign(told, {
      made: [],
      engines: [],
      characters: 0,
      makingFails: 0,
      loadingFails: 0,
      loads: 0,
    });
    vi.doMock("shiki", async (importOriginal) => {
      const shiki = await importOriginal<typeof Shiki>();
      return {
        ...shiki,
        createOnigurumaEngine: (
          ...given: Parameters<typeof shiki.createOnigurumaEngine>
        ) => {
          const engine = shiki.createOnigurumaEngine(...given);
          told.engines.push(engine);
          return engine;
        },
        createHighlighter: async (options: HighlighterOptions) => {
          told.made.push(options);
          if (told.makingFails > 0) {
            told.makingFails -= 1;
            throw new Error("The engine couldn't be fetched.");
          }
          const made = await shiki.createHighlighter(options);
          const { codeToTokens: tokens, loadLanguage: load } = made;
          made.codeToTokens = (code, more) => {
            told.characters += code.length;
            return tokens(code, more);
          };
          made.loadLanguage = async (...languages) => {
            told.loads += 1;
            if (told.loadingFails > 0) {
              told.loadingFails -= 1;
              throw new Error("The grammar couldn't be fetched.");
            }
            await load(...languages);
          };
          return made;
        },
      };
    });
    vi.resetModules();
    ({ colour } = await import("./colouring"));
  });

  afterEach(() => {
    vi.doUnmock("shiki");
  });

  test("the highlighter is made with the WebAssembly engine", async () => {
    await ask("const a = 1;");

    expect(told.engines).toHaveLength(1);
    expect(told.made).toHaveLength(1);
    expect(told.made[0]?.engine).toBe(told.engines[0]);
  });

  test("code that is arriving is coloured from where it had got, not from its start", async () => {
    const code = Array.from(
      { length: 120 },
      (_, at) =>
        `const part${String(at)} = "saddle"; // part ${String(at)} of the bike`,
    ).join("\n");
    // A few words at a time, as a model writes.
    let had = "";
    let lastLines = 0;
    let inAll = 0;
    for (let cut = 16; had !== code; cut += 16) {
      const piece = code.slice(0, cut);
      await ask(piece, { have: had.length });
      lastLines += piece.length - piece.lastIndexOf("\n") - 1;
      inAll += piece.length;
      had = piece;
    }

    // Each whole line once, and the line that was arriving once for each piece that
    // ended in it: far from colouring each piece from its start.
    const lineBreaks = code.split("\n").length - 1;
    expect(told.characters).toBeGreaterThanOrEqual(code.length - lineBreaks);
    expect(told.characters).toBeLessThanOrEqual(code.length + lastLines);
    expect(told.characters * 10).toBeLessThan(inAll);
  });

  test("only the last four pieces of a language are kept: an older one is coloured from its start again", async () => {
    // What is kept costs memory for as long as the page is open.
    const piece = (name: string) =>
      `const ${name} = 1;\nconst ${name}2 = 2;\nconst ${name}3`;
    const first = piece("saddle");
    for (const name of ["saddle", "chain", "wheel", "brake", "frame"]) {
      await ask(piece(name));
    }
    const before = told.characters;

    // Its whole lines were worked out, and are no longer kept.
    await ask(`${first} = 3;`, { have: first.length });

    const wholeLines = first.slice(0, first.lastIndexOf("\n"));
    expect(told.characters - before).toBeGreaterThanOrEqual(wholeLines.length);

    // One of the last four is gone on from: only its arriving line is coloured again.
    const kept = piece("frame");
    const beforeKept = told.characters;
    await ask(`${kept} = 3;`, { have: kept.length });
    expect(told.characters - beforeKept).toBeLessThan(wholeLines.length);
  });

  test("a language that couldn't be loaded is loaded again for the next block", async () => {
    told.loadingFails = 1;
    await expect(ask("const a = 1;")).rejects.toThrow(
      "The grammar couldn't be fetched.",
    );

    const { rows } = await ask("const b = 2;");

    expect(token(rows, "const").htmlStyle?.color).toMatch(/^#/);
  });

  test("a highlighter that couldn't be made is made again for the next block", async () => {
    told.makingFails = 1;
    await expect(ask("const a = 1;")).rejects.toThrow(
      "The engine couldn't be fetched.",
    );

    const { rows } = await ask("const b = 2;");

    expect(token(rows, "const").htmlStyle?.color).toMatch(/^#/);
    expect(told.made).toHaveLength(2);
  });

  test("what keeps failing is asked for three times in all, and not at every block", async () => {
    // As a grammar's file of an older version of the app, under a page that stayed
    // open: every piece of a block that arrives would ask for it again.
    told.loadingFails = 10;

    for (const code of [
      "const a",
      "const a = 1;",
      "let b",
      "let b = 2;",
      "c",
    ]) {
      await expect(ask(code)).rejects.toThrow(
        "The grammar couldn't be fetched.",
      );
    }

    expect(told.loads).toBe(3);
    // Another language is asked for as ever.
    told.loadingFails = 0;
    const { rows } = await ask("body { margin: 0 }", { language: "css" });
    expect(token(rows, "margin").htmlStyle?.color).toMatch(/^#/);
  });

  test("a highlighter that keeps failing to be made is made three times in all", async () => {
    told.makingFails = 10;

    for (const code of ["const a", "const a = 1;", "let b", "let b = 2;"]) {
      await expect(ask(code)).rejects.toThrow(
        "The engine couldn't be fetched.",
      );
    }

    expect(told.made).toHaveLength(3);
  });
});
