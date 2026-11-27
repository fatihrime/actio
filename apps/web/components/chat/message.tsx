import {
  Component,
  createContext,
  memo,
  use,
  useEffect,
  useSyncExternalStore,
} from "react";
import type { ReactNode } from "react";

import type { Model, Turn } from "@/lib/api/client";
import { figuresOf } from "@/lib/chat/figures";
import { isBlank, turnOf } from "@/lib/chat/messages";
import type { ChatMessage } from "@/lib/chat/messages";
import { waitingBehind, waitsBehind } from "@/lib/chat/waits";

// Its type alone: the code comes apart from the page, below.
import type Markdown from "./markdown";
import { usePaced } from "./paced";
import { useSettled } from "./settled";

// What turns markdown into headings, tables, code and formulas is large, so it is a
// piece apart from the page: the renderer. A page fetches it once: ahead, once it knows
// whose session it has and the list of conversations is on the page (`fetchRenderer`,
// called by ChatApp, ADR 0016, revision of 2026-11-22), or at once when something
// needs it before that: a question being written (chat.tsx), an answer on the page,
// before its first words, and a stored conversation (`useRenderer`).
//
// Once it is here, an answer is shown as markdown from its first render. While it is on
// its way, none of an answer's words is on the page: shown as written, they would be
// swapped for markdown a moment later, and the page would jump. The answer says that it
// waits, and a stored conversation that the page has is not shown yet (chat-app.tsx).
// But not for long: the page waits for the renderer once, `RENDERER_WAIT_MS` at the
// most, counted from the first moment something on it had words to show, and from then
// on whatever waited, and whatever comes after, is shown as written, to be markdown
// once the renderer is here (`useWaitsForRenderer`, ADR 0016, revision "an answer that
// arrives waits for the renderer too"). When the renderer can't be fetched (the
// installation was upgraded under an open page), words are shown as written at once,
// and for good. So is a piece of an answer whose markdown can't be drawn once it is
// here (`AsWrittenOnError`).

/** How long the page waits for the renderer at the most, by the clock, before what
 * waits for it is shown as written, to become markdown once it is here: a stored
 * conversation since ADR 0016's revision of 2026-11-21, and an answer that arrives since
 * its revision "an answer that arrives waits for the renderer too". On a slow
 * connection (1.6 Mbit/s) the renderer takes 1.7 to 2.4 seconds: a shorter wait would
 * show the marks there. A download that stalls must not hide words the page already
 * has. */
export const RENDERER_WAIT_MS = 3000;

/** How often, at the most, the page's wait goes without looking at the clock. A test
 * moves the clock (tests/clock.ts), and the page notices when it next looks. */
const CLOCK_LOOK_MS = 1000;

/** Where this page's renderer stands: on its way, here, or not to be had. Nothing
 * while nobody has asked for it, and on the server. */
type Renderer =
  | { state: "fetching" }
  | { state: "here"; Markdown: typeof Markdown }
  | { state: "gone" };

let fetched: Renderer | null = null;
// The page's one wait for the renderer: whether it has begun, whether it is over, and
// the timer that looks at the clock until then. Once over, it stays over.
let waitBegun = false;
let waitedOut = false;
let lookAgain: ReturnType<typeof setTimeout> | undefined;
const listeners = new Set<() => void>();

function tellListeners(): void {
  for (const listener of listeners) {
    listener();
  }
}

function become(now: Renderer): void {
  fetched = now;
  if (now.state !== "fetching") {
    // Nothing waits for it any more.
    clearTimeout(lookAgain);
  }
  tellListeners();
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

/** Begin the page's wait for the renderer, unless it has begun, or the renderer's
 * fetch is over. It ends `RENDERER_WAIT_MS` later by the clock, for everything that
 * waits, whether or not what began it is still on the page. */
function beginWait(): void {
  if (waitBegun || (fetched !== null && fetched.state !== "fetching")) {
    return;
  }
  waitBegun = true;
  const until = Date.now() + RENDERER_WAIT_MS;
  function look() {
    const left = until - Date.now();
    if (left > 0) {
      lookAgain = setTimeout(look, Math.min(left, CLOCK_LOOK_MS));
      return;
    }
    waitedOut = true;
    tellListeners();
  }
  lookAgain = setTimeout(look, Math.min(RENDERER_WAIT_MS, CLOCK_LOOK_MS));
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

/** Whether words that are to be shown wait for the renderer: while nobody has asked for
 * it or it is being fetched, until the page's wait for it is over. `words` says
 * whether there are any to show yet: the page's wait begins at the first moment that
 * something on it has words and no renderer, and is told to everything that waits when
 * it is over. The renderer is asked for, if nobody has yet. */
export function useWaitsForRenderer(words: boolean): boolean {
  const renderer = useRenderer();
  const over = useSyncExternalStore(
    listen,
    () => waitedOut,
    () => false,
  );
  const waits =
    words && (renderer === null || renderer.state === "fetching") && !over;
  useEffect(() => {
    if (waits) {
      beginWait();
    }
  }, [waits]);
  return waits;
}

/** The models on offer, as the page last read them, or `null` until it has: what an
 * answer took names the model that wrote it by its title there, and the one asked when
 * another wrote it, and says nothing of the asked model's own answer where it is the
 * one model on offer (`figuresOf`). Given to the conversation's messages by `Chat`, and
 * read by `Took` alone. A context, and no property of `Message`: the models are read
 * again while the model that answers can't, and that draws `Took` again, never the
 * answers' words. */
export const ModelsOnOffer = createContext<readonly Model[] | null>(null);

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
    // A question is shown as it was typed: someone who types a star means a star. Its
    // id is how the page finds it again, as the place where a reader left the
    // conversation (follow.ts).
    return (
      <article
        aria-label="You"
        data-message-id={message.id}
        className="flex justify-end"
      >
        <div className="max-w-[85%] rounded-2xl bg-muted px-4 py-2.5 wrap-break-word whitespace-pre-wrap forced-colors:border">
          {message.parts.map((part, index) =>
            part.type === "text" ? <span key={index}>{part.text}</span> : null,
          )}
        </div>
      </article>
    );
  }
  return <Answer message={message} writing={writing} />;
});

/** A model's answer: what it thought, its words, and what it took.
 *
 * Until something of it is drawn, it says that it waits for the model, or, while its
 * stream says so, behind how many other answers it waits at its model's gate
 * (`waitsBehind`, read from the message itself).
 *
 * None of them is on the page while the renderer is on its way, whether the answer is
 * still arriving or already complete, until the page's wait for it is over
 * (`useWaitsForRenderer`). Meanwhile the answer says what it says before its first
 * word, once. Then all of them are shown at once, as markdown from their first render,
 * or as written when the wait ran out. It asks for the renderer as soon as it is on the
 * page, before its first word.
 *
 * What the answer took comes under it, after its article, once its words are shown and
 * its turn has ended, as its record says (`Took`, `Figures`): nothing of it while the
 * answer arrives, and for an answer that arrived here, a frame after its last words.
 *
 * An answer that is over and has nothing in it to read draws nothing at all. */
function Answer({
  message,
  writing,
}: {
  message: ChatMessage;
  writing: boolean;
}) {
  const parts = message.parts.filter(
    (part) => part.type === "text" || part.type === "reasoning",
  );
  // A piece of text that has begun and has no words yet draws nothing, and until
  // something is drawn the answer goes on saying that it waits for the model. A piece
  // begins a moment before its first words: drawn from its beginning, it stood there
  // empty in place of the wait, for as long as its first words took, and the
  // conversation got shorter and then longer again under whoever was reading its end
  // (ADR 0015, revision "an answer waits for the model until it has words"). A thought
  // that has begun says so by itself.
  const drawn = (part: (typeof parts)[number]) =>
    part.type === "reasoning" || part.text.trim() !== "";
  const shown = parts.filter(drawn);
  // The last piece that is drawn is the one that can still be arriving. So a thought
  // stays open until the text after it has words, and folds away as they come: not a
  // moment before, with nothing yet in its place.
  const last = parts.map(drawn).lastIndexOf(true);
  const waits = useWaitsForRenderer(shown.length > 0);
  // Nothing of the answer is drawn yet: it may wait at its model's gate, and says so
  // when its stream said behind how many. Never while its words wait for the renderer,
  // nor once a thought or a word is drawn.
  const beginning = writing && shown.length === 0;
  if (!writing && isBlank(message)) {
    // An answer that is over and has nothing in it to read: nothing arrived, because
    // its turn failed before a word or completed with none, or nothing but blank
    // space did. There is nothing to draw, or to say under it. Why no answer came is
    // said in its place (`outcomeOf`, chat.tsx).
    return null;
  }
  // The article is the model's words and nothing else. What the page says about the
  // answer stands after it, as the notice of how a turn ended does (chat.tsx). Its id
  // is an attribute, and no text of it, as a question's is.
  return (
    <>
      <article
        aria-label="Actio"
        data-message-id={message.id}
        className="space-y-3"
      >
        {!waits &&
          parts.map((part, index) => {
            if (!drawn(part)) {
              return null;
            }
            const arriving = writing && index === last;
            return part.type === "reasoning" ? (
              <Reasoning key={index} text={part.text} writing={arriving} />
            ) : (
              <Words key={index} text={part.text} writing={arriving} />
            );
          })}
        {(beginning || waits) && (
          <Waiting behind={beginning ? waitsBehind(message) : 0} />
        )}
      </article>
      <Took turn={turnOf(message)} writing={writing} waits={waits} />
    </>
  );
}

/** What an answer took, once it is to be said: its words are shown, its turn has ended,
 * and, for an answer that arrived on this page, the frame with its last words has been
 * painted (`useSettled`, which says why). That wait is kept here, and not in `Answer`:
 * a frame later this is drawn again, and the answer's words are not. So are the models
 * on offer, which name the model that wrote the answer (`ModelsOnOffer`): read here,
 * they draw this again when they come, and nothing else of the answer. */
function Took({
  turn,
  writing,
  waits,
}: {
  turn: Turn | null;
  writing: boolean;
  waits: boolean;
}) {
  const settled = useSettled(writing);
  const models = use(ModelsOnOffer);
  const figures =
    settled && !waits && turn !== null ? figuresOf(turn, models) : [];
  return figures.length > 0 ? <Figures figures={figures} /> : null;
}

/** What an answer took, in one quiet line under it: how fast its model wrote, how long
 * its first word took, what it cost, and which model wrote it, where a reader could
 * take it for another's (`figuresOf`). There for everyone, not on hover,
 * which a phone and a keyboard don't have. A list a screen reader names, with an item a
 * figure; the dots between them are drawn, and have nothing to say. It is the answer's
 * next sibling in the log, which puts `gap-6` between its children: a margin of less
 * than nothing brings it to 8 px under its answer, so that it reads as that answer's.
 *
 * An item goes to the next row whole when it doesn't fit beside the one before it. One
 * wider than the whole row, as a model's long title can make it, shrinks to the row,
 * and its words fold there, inside a word when one word alone is wider than the row:
 * never wider than the conversation, which would scroll sideways. `wrap-anywhere`,
 * unlike `wrap-break-word`, lets the item's smallest width shrink with it. */
function Figures({ figures }: { figures: string[] }) {
  return (
    <ul
      // A list without its marks is no list to Safari's screen reader unless it says so.
      role="list"
      aria-label="What this answer took"
      // The log is a live region, which says what is added to it. This is no news:
      // it is there for whoever looks for it, and isn't read out after every answer.
      aria-live="off"
      className="-mt-4 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground"
    >
      {figures.map((figure, index) => (
        <li key={figure} className="flex items-center gap-2 wrap-anywhere">
          {index > 0 && (
            <span
              aria-hidden="true"
              // A dot is a ground and nothing else: where a system's colours take the
              // place of the page's, it has the colour of the words by its role
              // (app/globals.css).
              className="size-0.75 shrink-0 rounded-full bg-current forced-colors:bg-foreground"
            />
          )}
          {figure}
        </li>
      ))}
    </ul>
  );
}

/** A model's words, as markdown once the renderer is here, and as written when the
 * page's wait for it ran out or it can't be had, or when they can't be drawn. While
 * they arrive, they are drawn at most every `DRAWN_EVERY_MS`, and whole once they are
 * complete (`usePaced`). */
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

/** Stands in for an answer that hasn't begun: a model may first have to load. And for
 * one whose words wait for the renderer.
 *
 * Given `behind`, 1 or more, it says how many other answers this one waits behind at its
 * model's gate, in its place (`waitingBehind`). One element whose words change: a
 * screen reader is told of each change, which comes when an answer ends, or somebody
 * comes or leaves, and never by the clock. */
export function Waiting({ behind = 0 }: { behind?: number }) {
  return (
    <p
      role="status"
      className="animate-pulse text-sm text-muted-foreground motion-reduce:animate-none"
    >
      {behind > 0 ? waitingBehind(behind) : "Waiting for the model…"}
    </p>
  );
}
