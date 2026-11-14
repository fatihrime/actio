import type { ChatMessage } from "@/lib/chat/messages";

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
    return (
      <article aria-label="You" className="flex justify-end">
        <div className="max-w-[85%] rounded-2xl bg-zinc-100 px-4 py-2.5 wrap-break-word whitespace-pre-wrap dark:bg-zinc-800">
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
      {shown.map((part, index) =>
        part.type === "reasoning" ? (
          <Reasoning
            key={index}
            text={part.text}
            // Still reasoning: nothing has come after it yet.
            writing={writing && index === shown.length - 1}
          />
        ) : (
          <div key={index} className="wrap-break-word whitespace-pre-wrap">
            {part.text}
          </div>
        ),
      )}
      {writing && shown.length === 0 && <Waiting />}
    </article>
  );
}

/** What a model thought before it answered: shown while it arrives, folded away once
 * the answer begins, and there to open again. */
function Reasoning({ text, writing }: { text: string; writing: boolean }) {
  return (
    <details
      open={writing}
      className="group rounded-lg border border-zinc-200 text-sm text-zinc-600 dark:border-zinc-800 dark:text-zinc-400"
    >
      <summary className="cursor-pointer px-3 py-2 font-medium select-none">
        {writing ? "Thinking…" : "Thought before answering"}
      </summary>
      <div className="border-t border-zinc-200 px-3 py-2 wrap-break-word whitespace-pre-wrap dark:border-zinc-800">
        {text}
      </div>
    </details>
  );
}

/** Stands in for an answer that hasn't begun: a model may first have to load. */
export function Waiting() {
  return (
    <p
      role="status"
      className="animate-pulse text-sm text-zinc-500 motion-reduce:animate-none"
    >
      Waiting for the model…
    </p>
  );
}
