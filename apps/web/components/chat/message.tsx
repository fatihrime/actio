import { useEffect, useSyncExternalStore } from "react";

import type { ChatMessage } from "@/lib/chat/messages";

// Its type alone: the code comes apart from the page, below.
import type Markdown from "./markdown";

// What turns markdown into headings, tables, code and formulas is large, so it is a
// piece apart from the page: the renderer. A page fetches it once, as soon as it knows
// whose session it has (`fetchRenderer`, called by ChatApp), or when an answer is shown
// before that. Once it is here, an answer is shown as markdown from its first render.
// Until then, and for good if it can't be fetched (the installation was upgraded under
// an open page), an answer is shown as it was written (ADR 0016, revision of
// 2026-11-21).

/** Where this page's renderer stands: on its way, here, or not to be had. Nothing
 * while nobody has asked for it, and on the server. */
type Renderer =
  | { state: "fetching" }
  | { state: "here"; Markdown: typeof Markdown }
  | { state: "gone" };

let fetched: Renderer | null = null;
const listeners = new Set<() => void>();

function become(now: Renderer): void {
  fetched = now;
  for (const listener of listeners) {
    listener();
  }
}

/** Ask for the renderer, unless this page has already: whatever asks first fetches
 * it, and nothing asks twice. */
export function fetchRenderer(): void {
  if (fetched !== null) {
    return;
  }
  become({ state: "fetching" });
  void import("./markdown").then(
    (module) => {
      become({ state: "here", Markdown: module.default });
    },
    () => {
      become({ state: "gone" });
    },
  );
}

function listen(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** The renderer as it stands, asked for if nobody has yet. */
export function useRenderer(): Renderer | null {
  useEffect(fetchRenderer, []);
  return useSyncExternalStore(
    listen,
    () => fetched,
    () => null,
  );
}

/** One message of a conversation: a question, or a model's answer. */
export function Message({
  message,
  writing = false,
}: {
  message: ChatMessage;
  /** Whether this answer is still arriving. */
  writing?: boolean;
}) {
  if (message.role === "user") {
    // A question is shown as it was typed: someone who types a star means a star.
    return (
      <article aria-label="You" className="flex justify-end">
        <div className="max-w-[85%] rounded-2xl bg-muted px-4 py-2.5 wrap-break-word whitespace-pre-wrap">
          {message.parts.map((part, index) =>
            part.type === "text" ? <span key={index}>{part.text}</span> : null,
          )}
        </div>
      </article>
    );
  }
  const shown = message.parts.filter(
    (part) => part.type === "text" || part.type === "reasoning",
  );
  return (
    <article aria-label="Actio" className="space-y-3">
      {shown.map((part, index) => {
        // Only the last part can still be arriving.
        const arriving = writing && index === shown.length - 1;
        return part.type === "reasoning" ? (
          <Reasoning key={index} text={part.text} writing={arriving} />
        ) : (
          <Words key={index} text={part.text} writing={arriving} />
        );
      })}
      {writing && shown.length === 0 && <Waiting />}
    </article>
  );
}

/** A model's words, as markdown once the renderer is here, and as written until then. */
function Words({
  text,
  writing,
  className,
}: {
  text: string;
  writing: boolean;
  className?: string;
}) {
  const renderer = useRenderer();
  return renderer?.state === "here" ? (
    <renderer.Markdown text={text} writing={writing} className={className} />
  ) : (
    <AsWritten text={text} className={className} />
  );
}

/** A model's words, exactly as they were written. */
function AsWritten({ text, className }: { text: string; className?: string }) {
  return (
    <div className={`wrap-break-word whitespace-pre-wrap ${className ?? ""}`}>
      {text}
    </div>
  );
}

/** What a model thought before it answered: shown while it arrives, folded away once
 * the answer begins, and there to open again. */
function Reasoning({ text, writing }: { text: string; writing: boolean }) {
  return (
    <details
      open={writing}
      className="group rounded-lg border border-border text-sm text-muted-foreground"
    >
      <summary className="cursor-pointer px-3 py-2 font-medium select-none">
        {writing ? "Thinking…" : "Thought before answering"}
      </summary>
      <div className="border-t border-border px-3 py-2">
        <Words
          text={text}
          writing={writing}
          // A heading in a thought is no louder than the thought.
          className="[&_:is(h1,h2,h3,h4,h5,h6)]:mt-3 [&_:is(h1,h2,h3,h4,h5,h6)]:text-sm"
        />
      </div>
    </details>
  );
}

/** Stands in for an answer that hasn't begun: a model may first have to load. */
export function Waiting() {
  return (
    <p
      role="status"
      className="animate-pulse text-sm text-muted-foreground motion-reduce:animate-none"
    >
      Waiting for the model…
    </p>
  );
}
