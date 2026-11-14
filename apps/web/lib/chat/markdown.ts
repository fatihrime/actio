// Where this app reads an answer's markdown differently than the markdown library
// does. Each change comes from what models really write (ADR 0016), and each is made
// on the parsed text, so none ever touches what is inside a piece of code.

/** The fields of a piece of parsed markdown that hold words. */
const WORDS = [
  "value",
  "url",
  "title",
  "alt",
  "lang",
  "meta",
  "label",
  "identifier",
] as const;

/** A piece of parsed markdown, as far as this file looks at it. */
type Piece = {
  type: string;
  children?: Piece[];
  position?: { start: { offset?: number }; end: { offset?: number } };
  data?: Record<string, unknown> | undefined;
} & { [Field in (typeof WORDS)[number]]?: string | null };

/** The text that was parsed. */
interface Source {
  toString: () => string;
}

// --- HTML ---------------------------------------------------------------------------

const LINE_BREAK = /^<br\s*\/?>$/i;
const ONLY_LINE_BREAKS = /^(<br\s*\/?>\s*)+$/i;
/** The pieces that hold whole blocks, such as paragraphs, and not words. */
const HOLDS_BLOCKS = new Set([
  "root",
  "blockquote",
  "listItem",
  "footnoteDefinition",
]);

/** Shows HTML as it was written.
 *
 * HTML in an answer is never obeyed: the library turns a tag between words into text.
 * Two cases are handled here, before it does.
 *
 * - HTML that stands on its own lines becomes a block of code, so a page that a model
 *   wrote without fencing it keeps its lines.
 * - `<br>` is the one tag that is obeyed, in the two places where it can only mean
 *   space. In a table's cell it becomes a line break: a cell can't hold a new line in
 *   markdown, so models write `<br>` there. On a line of its own it becomes an empty
 *   paragraph.
 */
export function htmlAsWritten() {
  return (tree: Piece): void => {
    each(tree, (piece, parent) => {
      if (piece.type === "tableCell") {
        each(piece, (inside) => {
          if (inside.type === "html" && LINE_BREAK.test(inside.value ?? "")) {
            inside.type = "break";
            delete inside.value;
          }
        });
      } else if (
        piece.type === "html" &&
        HOLDS_BLOCKS.has(parent?.type ?? "")
      ) {
        if (ONLY_LINE_BREAKS.test(piece.value?.trim() ?? "")) {
          piece.type = "paragraph";
          piece.children = [];
          delete piece.value;
        } else {
          piece.type = "code";
          piece.lang = "html";
        }
      }
    });
  };
}

// --- Dollar signs -------------------------------------------------------------------

/** Stands for a dollar sign that was written with a backslash before it, from before
 * the text is parsed until after. Nobody types this character: it is from the part
 * of Unicode that is set aside for private use. */
const ESCAPED = "";

/** The pieces whose words are kept exactly as written, backslashes and all. */
const KEPT_AS_WRITTEN = new Set([
  "code",
  "inlineCode",
  "html",
  "math",
  "inlineMath",
]);

/** Sets the escaped dollar signs of `text` aside, so the parser can't take one for
 * the end of a formula. `dollars` puts them back.
 *
 * Asked what two coffees cost, a model writes `$4 \times 2 = \$8$`. The parser knows
 * no escapes inside a formula, and ends this one at the second dollar sign.
 */
export function setEscapedDollarsAside(text: string): string {
  // A backslash before a backslash is an escaped backslash, and says nothing about
  // what follows the pair.
  return text.replace(/\\(\\|\$)/g, (pair, second) =>
    second === "$" ? ESCAPED : pair,
  );
}

/** Reads dollar signs as people mean them. For text that went through
 * `setEscapedDollarsAside`.
 *
 * Models write a formula between single dollar signs, so that is read as a formula.
 * But "$5 and $10" is money. The rule that tells them apart is pandoc's: a formula
 * has no space just inside its dollar signs, and no digit right after the last one.
 * Anything else between two dollar signs goes back to being text.
 *
 * A formula between double dollar signs is meant to stand on its own line. Models
 * write it in two ways the markdown parser reads otherwise:
 *
 * - in the middle of a line, or as a line of its own with both pairs of dollar signs
 *   on it. The parser takes that for a formula between words;
 * - begun on the line of its opening dollar signs. The parser takes the rest of that
 *   line for a label, as after the marks that open a block of code, and drops it. A
 *   formula that is still arriving, or was cut off, looks like this too: without this
 *   it would show as nothing.
 */
export function dollars() {
  return (tree: Piece, file: Source): void => {
    const source = file.toString();
    each(tree, (piece) => {
      putEscapedDollarsBack(piece);
      if (piece.type === "math") {
        const formula = [piece.meta, piece.value].filter(Boolean).join("\n");
        piece.value = formula;
        piece.meta = null;
        piece.data = onItsOwnLines(formula);
      } else if (piece.type === "inlineMath") {
        const start = piece.position?.start.offset ?? 0;
        const end = piece.position?.end.offset ?? 0;
        const written = source.slice(start, end);
        const signs = /^\$+/.exec(written)?.[0].length ?? 0;
        const inside = written.slice(signs, written.length - signs);
        if (signs >= 2) {
          piece.data = onItsOwnLines(piece.value ?? "");
        } else if (/^\s|\s$/.test(inside) || /\d/.test(source.charAt(end))) {
          piece.type = "text";
          piece.value = written.replaceAll(ESCAPED, "$");
          delete piece.data;
        } else {
          piece.data = betweenWords(piece.value ?? "");
        }
      }
    });
  };
}

function putEscapedDollarsBack(piece: Piece): void {
  for (const field of WORDS) {
    const words = piece[field];
    if (typeof words === "string" && words.includes(ESCAPED)) {
      // Where words are kept as written, the backslash is one of them. Anywhere
      // else it only said that the dollar sign is a dollar sign.
      const asWritten = field === "value" && KEPT_AS_WRITTEN.has(piece.type);
      piece[field] = words.replaceAll(ESCAPED, asWritten ? "\\$" : "$");
    }
  }
}

// What the parser hands the formula renderer: code, marked as a formula. A formula on
// its own lines is in a `pre`, and the renderer sets it apart from the text around.

function betweenWords(formula: string): Record<string, unknown> {
  return {
    hName: "code",
    hProperties: { className: ["language-math", "math-inline"] },
    hChildren: [{ type: "text", value: formula }],
  };
}

function onItsOwnLines(formula: string): Record<string, unknown> {
  return {
    hName: "pre",
    hChildren: [
      {
        type: "element",
        tagName: "code",
        properties: { className: ["language-math", "math-display"] },
        children: [{ type: "text", value: formula }],
      },
    ],
  };
}

function each(
  piece: Piece,
  visit: (piece: Piece, parent: Piece | null) => void,
  parent: Piece | null = null,
): void {
  visit(piece, parent);
  for (const child of piece.children ?? []) {
    each(child, visit, piece);
  }
}
