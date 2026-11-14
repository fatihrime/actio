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
      className="rounded-2xl border border-input bg-card p-2 shadow-sm focus-within:border-ring"
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
        className="field-sizing-content max-h-60 w-full resize-none bg-transparent px-2 py-1.5 outline-none placeholder:text-muted-foreground"
      />
      <div className="flex items-center justify-between gap-3 px-1">
        {thinking === null ? (
          <span />
        ) : (
          <label className="flex cursor-pointer items-center gap-2 text-sm text-muted-foreground select-none">
            <input
              type="checkbox"
              checked={thinking.on}
              onChange={(event) => {
                thinking.set(event.target.checked);
              }}
              className="size-4 accent-primary"
            />
            Think before answering
          </label>
        )}
        {answering ? (
          <button
            type="button"
            onClick={onStop}
            className="rounded-lg border border-input px-4 py-1.5 text-sm font-medium hover:bg-muted focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
          >
            Stop
          </button>
        ) : (
          <button
            type="submit"
            disabled={!sendable}
            title={blocked ?? undefined}
            className="rounded-lg bg-primary px-4 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/80 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring disabled:cursor-not-allowed disabled:opacity-40"
          >
            Send
          </button>
        )}
      </div>
    </form>
  );
}
