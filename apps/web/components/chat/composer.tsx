import type {
  KeyboardEvent,
  ReactElement,
  RefObject,
  SyntheticEvent,
} from "react";

/** Where a question is written and sent, or the running answer stopped. */
export function Composer({
  box,
  text,
  onText,
  onSend,
  onStop,
  answering,
  blocked,
  thinking,
  picker = null,
}: {
  /** The box to write in, for whoever wants to put the keyboard there. */
  box?: RefObject<HTMLTextAreaElement | null>;
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
  /** The picker of the model that answers, when there is a choice: first in the row,
   * with the switch for reasoning to its right, so that the switch, which comes and
   * goes with the model, never moves it. */
  picker?: ReactElement | null;
}) {
  const sendable = !answering && blocked === null && text.trim() !== "";

  // Beside the picker the switch keeps to one line, and says less in little room:
  // "Think" alone, until the room the picker leaves it in the row holds all of its
  // words (a container query, as the line under the box has, on the place the switch
  // stands in). Its name is the same whatever it shows. Without the picker it is as it
  // was before there was one.
  const reasoning =
    thinking === null ? null : (
      <label
        className={
          picker === null
            ? "flex cursor-pointer items-center gap-2 text-sm text-muted-foreground select-none"
            : "flex shrink-0 cursor-pointer items-center gap-2 text-sm whitespace-nowrap text-muted-foreground select-none"
        }
      >
        <input
          type="checkbox"
          checked={thinking.on}
          onChange={(event) => {
            thinking.set(event.target.checked);
          }}
          aria-label={picker === null ? undefined : "Think before answering"}
          className="size-4 accent-primary"
        />
        {picker === null ? (
          "Think before answering"
        ) : (
          <span>
            Think
            <span className="hidden @[12.5rem]:inline"> before answering</span>
          </span>
        )}
      </label>
    );

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
        ref={box}
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
        {picker === null ? (
          (reasoning ?? <span />)
        ) : (
          // One line in any width. The switch's place takes the room the picker leaves,
          // and never less than "Think" needs: the switch's words give way first, and
          // then the picker's title, which ends in "…".
          <div className="flex min-w-0 flex-1 items-center gap-3">
            {picker}
            {reasoning !== null && (
              <div className="@container flex min-w-15 flex-1">{reasoning}</div>
            )}
          </div>
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
            // A border as wide as Stop's, which shows as part of the button's own face:
            // the box is as large with either button in it, so nothing moves when an
            // answer begins or ends.
            className="rounded-lg border border-transparent bg-primary px-4 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/80 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring disabled:cursor-not-allowed disabled:opacity-40"
          >
            Send
          </button>
        )}
      </div>
    </form>
  );
}
