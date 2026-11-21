// Colours code with shiki, in the worker the page starts for it (colouring.worker.ts).
// On the page's own thread, the first block of TypeScript after a page load kept the
// page still for 1.5 s in WebKit, while shiki made its patterns and first matched with
// them (ADR 0016, revision "code is coloured in a worker"). So this is the one file of
// the web app that imports shiki, apart from its test, and ESLint holds that: the page
// asks through lib/chat/code-colours.ts.
import {
  bundledLanguagesInfo,
  createHighlighter,
  createOnigurumaEngine,
} from "shiki";
import type {
  BundledLanguage,
  BundledTheme,
  GrammarState,
  Highlighter,
  ThemedToken,
} from "shiki";
import type { HighlightResult, HighlightToken } from "streamdown";

/** What the page asks about a block of code. */
export interface Question {
  code: string;
  /** As the block names it: a language's name or one of its aliases. */
  language: string;
  /** The light theme's name, and the dark one's. */
  themes: readonly [string, string];
  /** How much of `code`, from its start, the page has the rows of already. */
  have: number;
}

/** The colours of a block of code, without the rows the page has already. */
export interface Colours extends Omit<HighlightResult, "tokens"> {
  /** How many of its rows the page keeps: those of the whole lines in what it has. */
  keep: number;
  /** The rows after those, a row a line, as Streamdown draws them. */
  rows: HighlightToken[][];
}

/** A question, as the page sends it to the worker. */
export interface Asked {
  id: number;
  question: Question;
}

/** The worker's answer to the question of that id. */
export type Answered =
  { id: number; colours: Colours } | { id: number; error: string };

/** The themes the highlighter is made with: those Streamdown asks for. */
const THEMES: BundledTheme[] = ["github-light", "github-dark"];

/** What shiki calls a language that it doesn't colour. */
const TEXT = "text";

type Language = BundledLanguage | typeof TEXT;

/** Each language shiki knows, by its name and by each of its aliases. */
const LANGUAGES = new Map(
  bundledLanguagesInfo.flatMap(({ id, aliases = [] }) =>
    [id, ...aliases].map((name) => [name, id as BundledLanguage] as const),
  ),
);

/** The language a block names, as shiki knows it: plain text when it doesn't. */
function languageOf(named: string): Language {
  return LANGUAGES.get(named.trim().toLowerCase()) ?? TEXT;
}

let highlighter: Promise<Highlighter> | undefined;
const loading = new Map<Language, Promise<void>>();

/** How often one thing is asked for in all, when asking for it fails. What couldn't be
 * fetched once, as when the network broke off, may be there the next time; what is
 * gone, as a file of an older version of the app under a page that stayed open, is not
 * asked for again at every piece of code that arrives. */
const TRIES = 3;

/** How often asking for each thing has failed. */
const failures = new Map<string, number>();

/** The promise, and `forget` called if it fails, so that the next block that needs
 * `what` asks for it again: until it has failed `TRIES` times. */
function forgottenIfFailed<T>(
  what: string,
  promise: Promise<T>,
  forget: () => void,
): Promise<T> {
  void promise.catch(() => {
    const failed = (failures.get(what) ?? 0) + 1;
    failures.set(what, failed);
    if (failed < TRIES) {
      forget();
    }
  });
  return promise;
}

/** This worker's one highlighter, with the language loaded: each language the first
 * time it is asked for. */
async function ready(language: Language): Promise<Highlighter> {
  highlighter ??= forgottenIfFailed(
    "the highlighter",
    createHighlighter({
      themes: THEMES,
      langs: [],
      engine: createOnigurumaEngine(import("shiki/wasm")),
    }),
    () => {
      highlighter = undefined;
    },
  );
  const made = await highlighter;
  let loaded = loading.get(language);
  if (loaded === undefined) {
    loaded = forgottenIfFailed(
      `the language ${language}`,
      made.loadLanguage(language),
      () => {
        loading.delete(language);
      },
    );
    loading.set(language, loaded);
  }
  await loaded;
  return made;
}

/** The whole lines of a piece of code that was arriving, as they were worked out. */
interface Lines {
  /** The code up to the end of its last line break. */
  text: string;
  /** A row for each of those lines. */
  rows: ThemedToken[][];
  /** The grammar's state after the last of them. */
  state: GrammarState | undefined;
}

/** How many pieces of code are kept for each language: those last asked about. */
const KEPT_PER_LANGUAGE = 4;

/** By language and themes, the pieces of code kept, the last asked about first. */
const kept = new Map<string, Lines[]>();

/** The rows, with their offsets in the code moved `by` further. */
function moved(rows: ThemedToken[][], by: number): ThemedToken[][] {
  for (const row of rows) {
    for (const token of row) {
      token.offset += by;
    }
  }
  return rows;
}

/** The text without the line break it ends with, which ends its last line: shiki would
 * see a line after it, and the grammar's state after that line. */
function lastBreakOff(text: string): string {
  return text.slice(0, text.endsWith("\r\n") ? -2 : -1);
}

/**
 * The colours of a block of code, in rows of tokens as Streamdown draws them.
 *
 * Code that is arriving is asked about again with each piece that comes. So the rows of
 * its whole lines are kept, with the grammar's state after them, and the next piece is
 * coloured from there: a line's colours depend on the lines before it alone. Only the
 * rows the page doesn't have are given back.
 */
export async function colour({
  code,
  language,
  themes,
  have,
}: Question): Promise<Colours> {
  const lang = languageOf(language);
  const shiki = await ready(lang);
  const options = { lang, themes: { light: themes[0], dark: themes[1] } };
  const sort = [lang, ...themes].join("\n");
  const pieces = kept.get(sort) ?? [];
  kept.set(sort, pieces);

  // The longest piece kept that this code begins with.
  let from: Lines | undefined;
  for (const piece of pieces) {
    if (
      code.startsWith(piece.text) &&
      piece.text.length > (from?.text.length ?? -1)
    ) {
      from = piece;
    }
  }
  if (from !== undefined) {
    pieces.splice(pieces.indexOf(from), 1);
  }
  let lines: Lines = from ?? { text: "", rows: [], state: undefined };
  const whole = code.lastIndexOf("\n") + 1;
  if (whole > lines.text.length) {
    const more = shiki.codeToTokens(
      lastBreakOff(code.slice(lines.text.length, whole)),
      { ...options, grammarState: lines.state },
    );
    lines = {
      text: code.slice(0, whole),
      rows: lines.rows.concat(moved(more.tokens, lines.text.length)),
      state: more.grammarState,
    };
  }
  if (lines.text !== "") {
    pieces.unshift(lines);
    pieces.splice(KEPT_PER_LANGUAGE);
  }

  const last = shiki.codeToTokens(code.slice(whole), {
    ...options,
    grammarState: lines.state,
  });
  // The page has a row for each whole line in what it has, and one for the line that
  // was arriving, which may have grown since.
  const keep = code.slice(0, have).split("\n").length - 1;
  return {
    fg: last.fg,
    bg: last.bg,
    rootStyle: last.rootStyle,
    keep,
    rows: lines.rows.slice(keep).concat(moved(last.tokens, whole)),
  };
}
