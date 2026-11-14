import type { KeyboardEvent, SyntheticEvent } from "react";

/** Where a question is written and sent, or the running answer stopped. */
export function Composer({
  text,
  onText,
  onSend,
  onStop,
  answering,
  blocked,
  thinking,
}: {
  text: string;
  onText: (text: string) => void;
  onSend: () => void;
  onStop: () => void;
  /** Whether an answer is being written: then the button stops it. */
  answering: boolean;
  /** Why nothing can be sent right now, if so. Writing stays possible. */
  blocked: string | null;
  /** The switch for reasoning, when the model can reason. */
  thinking: { on: boolean; set: (on: boolean) => void } | null;
}) {
  const sendable = !answering && blocked === null && text.trim() !== "";

  function submit(event: SyntheticEvent) {
    event.preventDefault();
    if (sendable) {
      onSend();
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    // Enter sends; Shift+Enter starts a new line. While an input method is composing
    // a character (Japanese, Chinese, Korean), Enter belongs to it.
    if (
      event.key === "Enter" &&
      !event.shiftKey &&
      !event.nativeEvent.isComposing
    ) {
      submit(event);
    }
  }

  return (
    <form
      onSubmit={submit}
      className="rounded-2xl border border-zinc-300 bg-white p-2 shadow-sm focus-within:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-900 dark:focus-within:border-zinc-400"
    >
      <textarea
        aria-label="Message"
        placeholder="Ask anything"
        rows={2}
        value={text}
        onChange={(event) => {
          onText(event.target.value);
        }}
        onKeyDown={onKeyDown}
        // As tall as its text, up to a limit, where it scrolls. Browsers that don't
        // know `field-sizing` keep the two rows.
        className="field-sizing-content max-h-60 w-full resize-none bg-transparent px-2 py-1.5 outline-none placeholder:text-zinc-400"
      />
      <div className="flex items-center justify-between gap-3 px-1">
        {thinking === null ? (
          <span />
        ) : (
          <label className="flex cursor-pointer items-center gap-2 text-sm text-zinc-600 select-none dark:text-zinc-400">
            <input
              type="checkbox"
              checked={thinking.on}
              onChange={(event) => {
                thinking.set(event.target.checked);
              }}
              className="size-4 accent-zinc-700 dark:accent-zinc-300"
            />
            Think before answering
          </label>
        )}
        {answering ? (
          <button
            type="button"
            onClick={onStop}
            className="rounded-lg border border-zinc-300 px-4 py-1.5 text-sm font-medium hover:bg-zinc-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-zinc-500 dark:border-zinc-600 dark:hover:bg-zinc-800"
          >
            Stop
          </button>
        ) : (
          <button
            type="submit"
            disabled={!sendable}
            title={blocked ?? undefined}
            className="rounded-lg bg-zinc-900 px-4 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-zinc-500 disabled:cursor-not-allowed disabled:opacity-40 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
          >
            Send
          </button>
        )}
      </div>
    </form>
  );
}
