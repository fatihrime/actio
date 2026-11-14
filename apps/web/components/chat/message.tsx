import { lazy, Suspense } from "react";

import type { ChatMessage } from "@/lib/chat/messages";

// What turns markdown into headings, tables, code and formulas is large. It is fetched
// when the first answer is shown. Until it is there, and if it can't be fetched (the
// installation was upgraded under an open page), an answer is shown as it was written.
const Markdown = lazy(() =>
  import("./markdown").catch(() => ({ default: AsWritten })),
);

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

/** A model's words, as markdown. */
function Words({
  text,
  writing,
  className,
}: {
  text: string;
  writing: boolean;
  className?: string;
}) {
  return (
    <Suspense fallback={<AsWritten text={text} className={className} />}>
      <Markdown text={text} writing={writing} className={className} />
    </Suspense>
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
