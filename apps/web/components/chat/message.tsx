import { Component, memo, useEffect, useSyncExternalStore } from "react";
import type { ReactNode } from "react";

import type { ChatMessage } from "@/lib/chat/messages";

// Its type alone: the code comes apart from the page, below.
import type Markdown from "./markdown";
import { usePaced } from "./paced";

// What turns markdown into headings, tables, code and formulas is large, so it is a
// piece apart from the page: the renderer. A page fetches it once: ahead, once it knows
// whose session it has and the list of conversations is on the page (`fetchRenderer`,
// called by ChatApp, ADR 0016, revision of 2026-11-22), or at once when something
// needs it before that: a question being written (chat.tsx), and an answer or a stored
// conversation that waits for it on the page (`useRenderer`). Once it is here, an answer is shown as markdown from its first
// render. Until then, and for good if it can't be fetched (the installation was
// upgraded under an open page), an answer is shown as it was written (ADR 0016,
// revision of 2026-11-21). So is a piece of an answer whose markdown can't be drawn
// once it is here (`AsWrittenOnError`).

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

/** One message of a conversation: a question, or a model's answer.
 *
 * Drawn again only when its message or `writing` changed (`memo`): not at each step of
 * a long conversation coming onto the page, each key pressed in the box, or each piece
 * of an answer arriving below it. So nothing that is made anew at every render may be
 * passed to it. */
export const Message = memo(function Message({
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
});

/** A model's words, as markdown once the renderer is here, and as written until then,
 * or when they can't be drawn. While they arrive, they are drawn at most every
 * `DRAWN_EVERY_MS`, and whole once they are complete (`usePaced`). */
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
  const paced = usePaced(text, writing);
  return renderer?.state === "here" ? (
    <AsWrittenOnError text={paced} writing={writing} className={className}>
      <renderer.Markdown text={paced} writing={writing} className={className} />
    </AsWrittenOnError>
  ) : (
    <AsWritten text={paced} className={className} />
  );
}

/** What drawing a piece of a model's words as markdown came to. */
interface Drawing {
  /** It threw while the words were arriving. */
  failedArriving: boolean;
  /** It threw once they were complete. */
  failedComplete: boolean;
  /** It has just thrown, and isn't noted yet as one of the two. */
  caught: boolean;
}

/** A piece of a model's words drawn as markdown (`children`), or shown as written in
 * its place when drawing it throws: a part of the library that couldn't be fetched, or
 * a mistake of the library or of a plugin on some text a model wrote. Then that piece
 * alone is as written, and the rest of the page goes on (ADR 0016, revision "an answer
 * that can't be drawn is shown as written").
 *
 * While the words arrive, each piece of them is shown as written, and drawing them is
 * not tried again at each piece: a part that couldn't be fetched fails again each
 * time, and React writes each error it catches to the console. Once they are complete
 * it is tried once more, since what threw on half of something may draw whole. Words
 * that never arrive, a stored answer's, are tried once. Nothing else is said of it:
 * React reports the error to the console itself. */
class AsWrittenOnError extends Component<
  { text: string; writing: boolean; className?: string; children: ReactNode },
  Drawing
> {
  override state: Drawing = {
    failedArriving: false,
    failedComplete: false,
    caught: false,
  };

  static getDerivedStateFromError(): Partial<Drawing> {
    return { caught: true };
  }

  // React calls this before every render, the one that follows a caught error too,
  // with the props the error was thrown at: they say whether the words were arriving.
  static getDerivedStateFromProps(
    { writing }: { writing: boolean },
    { caught }: Drawing,
  ): Partial<Drawing> | null {
    if (!caught) {
      return null;
    }
    return writing
      ? { caught: false, failedArriving: true }
      : { caught: false, failedComplete: true };
  }

  override render(): ReactNode {
    const { text, writing, className, children } = this.props;
    const { failedArriving, failedComplete } = this.state;
    return failedComplete || (failedArriving && writing) ? (
      <AsWritten text={text} className={className} />
    ) : (
      children
    );
  }
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
