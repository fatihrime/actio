"use client";

import { code } from "@streamdown/code";
import { createMathPlugin } from "@streamdown/math";
import { useMemo } from "react";
import { harden } from "rehype-harden";
import {
  defaultRehypePlugins,
  defaultRemarkPlugins,
  Streamdown,
} from "streamdown";
import type {
  DiagramPlugin,
  MermaidErrorComponentProps,
  StreamdownProps,
} from "streamdown";

import {
  dollars,
  htmlAsWritten,
  setEscapedDollarsAside,
} from "@/lib/chat/markdown";
import { useDarkScheme } from "@/lib/scheme";

import "katex/dist/katex.min.css";

type Plugins = NonNullable<StreamdownProps["rehypePlugins"]>;

/** One of the library's own plugins. An upgrade that renames one must fail here, and
 * not leave an answer unchecked. */
function builtIn(plugins: Record<string, Plugins[number]>, name: string) {
  const plugin = plugins[name];
  if (plugin === undefined) {
    throw new Error(`Streamdown has no "${name}" plugin any more.`);
  }
  return plugin;
}

// What an answer may make the browser do (ADR 0016). A model's words are not to be
// trusted: they can come from a page it read, or a file someone gave it.
//
// - No HTML. The library's "raw" plugin is left out, so a tag is shown as text.
// - A link opens only after the reader has seen its address, and only to the web or
//   to an email address.
// - No picture is shown: fetching one tells whoever owns its address what the
//   address says, and an address can be written to carry what was said here.
const SAFE: Plugins = [
  builtIn(defaultRehypePlugins, "sanitize"),
  [
    harden,
    {
      allowedLinkPrefixes: ["*"],
      allowedProtocols: [],
      allowedImagePrefixes: [],
      allowDataImages: false,
      blockedLinkClass: "text-muted-foreground",
      blockedImageClass:
        "inline-block rounded bg-muted px-2 py-0.5 text-sm text-muted-foreground",
    },
  ],
];

const READ: Plugins = [
  ...Object.values(defaultRemarkPlugins),
  htmlAsWritten,
  dollars,
];

/** Draws diagrams, in light or dark colours. The library that does it is large, so it
 * is fetched when the first diagram is drawn. */
function diagrams(theme: "default" | "dark"): DiagramPlugin {
  return {
    name: "mermaid",
    type: "diagram",
    language: "mermaid",
    getMermaid: () => ({
      initialize: () => undefined,
      render: async (id, source) => {
        const { mermaid } = await import("@streamdown/mermaid");
        return mermaid.getMermaid({ theme }).render(id, source);
      },
    }),
  };
}

// Models write a formula between single dollar signs. `dollars` keeps money out.
const math = createMathPlugin({ singleDollarTextMath: true });
const LIGHT = { code, math, mermaid: diagrams("default") };
const DARK = { code, math, mermaid: diagrams("dark") };

// A link that is still arriving is shown as its words, not as a link to nowhere.
const HALF_ARRIVED = { linkMode: "text-only" } as const;

const DIAGRAMS = { errorComponent: NoDiagram };

/** A model's words, shown as the markdown they are written in: headings, lists,
 * tables, code, formulas and diagrams. */
export default function Markdown({
  text,
  writing,
  className,
}: {
  text: string;
  /** Whether the text is still arriving. Its last piece may then be half of
   * something, and is shown as what it is becoming. */
  writing: boolean;
  className?: string;
}) {
  const dark = useDarkScheme();
  const read = useMemo(() => setEscapedDollarsAside(text), [text]);
  return (
    <Streamdown
      className={className}
      isAnimating={writing}
      plugins={dark ? DARK : LIGHT}
      rehypePlugins={SAFE}
      remarkPlugins={READ}
      remend={HALF_ARRIVED}
      mermaid={DIAGRAMS}
      lineNumbers={false}
      codeBlockMaxHeight={0}
      tableMaxHeight={0}
    >
      {read}
    </Streamdown>
  );
}

/** Stands in for a diagram that can't be drawn. Models often get a diagram's syntax
 * wrong, so this shows what they wrote. */
function NoDiagram({ chart }: MermaidErrorComponentProps) {
  return (
    <div className="space-y-2 p-4">
      <p className="text-sm text-muted-foreground">
        This diagram can&apos;t be drawn as it was written.
      </p>
      <pre className="overflow-x-auto text-sm">
        <code>{chart}</code>
      </pre>
    </div>
  );
}
