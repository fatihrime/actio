"use client";

import { useChat } from "@ai-sdk/react";
import {
  useCallback,
  useEffect,
  useEffectEvent,
  useMemo,
  useRef,
  useState,
} from "react";

import { Notice, NoticeButton } from "@/components/notice";
import { LimitReached, UsagePassed } from "@/components/usage/limit";
import { ApiProblem, cancelTurn, getConversation } from "@/lib/api/client";
import type { Conversation, Model, Turn } from "@/lib/api/client";
import { fromStored, hasContent, turnOf } from "@/lib/chat/messages";
import type { ChatMessage } from "@/lib/chat/messages";
import {
  canRetry,
  explain,
  isSessionEnd,
  notReady,
  outcomeOf,
} from "@/lib/chat/outcomes";
import { turnTransport } from "@/lib/chat/turns";
import type { JoinOptions, TurnOptions } from "@/lib/chat/turns";
import { newId } from "@/lib/ids";
import { useStoredSwitch } from "@/lib/stored-switch";
import type { UsageLimits } from "@/lib/usage";

import { Composer } from "./composer";
import { useFollow } from "./follow";
import { Message, Waiting } from "./message";

const SETUP_GUIDE = "https://fatihrime.github.io/actio/getting-started/#models";
const THINKING_KEY = "actio.thinking";

/** How often to look whether an answer that is being written, and that this page
 * couldn't read along with, is done. */
const ELSEWHERE_MS = 1500;

/** A turn that is being written, and that this page isn't reading: it tries once to
 * read along with it. */
interface Join {
  /** The turn's id. */
  turn: string;
  /** Whether the try is over. However it went, that turn isn't tried again while the
   * conversation is on the page: a stream that is gone stays gone (ADR 0022). */
  over: boolean;
}

/** The try at reading along that what the server calls unanswered asks for, after
 * `tried`: a new one for a turn that is being written and wasn't tried yet, and
 * `tried` otherwise. */
function joinFor(unanswered: Turn | null, tried: Join | null): Join | null {
  return unanswered?.status === "running" && unanswered.id !== tried?.turn
    ? { turn: unanswered.id, over: false }
    : tried;
}

/** The conversation as the server has it, or nothing when it has none: its first
 * question never arrived. */
async function readConversation(id: string): Promise<Conversation | null> {
  try {
    return await getConversation(id);
  } catch (error) {
    if (error instanceof ApiProblem && error.status === 404) {
      return null;
    }
    throw error;
  }
}

/** One conversation: its messages, and the box to continue it.
 *
 * The messages on the page are the chat library's (`useChat`): it streams an answer
 * into them as it arrives. The server keeps the conversation itself, so when a turn
 * ends in any way but well (stopped, failed, refused, connection lost), the page asks
 * the server what it has and shows that, instead of guessing.
 *
 * An answer is written at the server whether or not this page reads it (ADR 0022).
 * Leaving the conversation ends the reading, and nothing else. Only Stop ends the
 * answer: the page then tells the server to stop the turn.
 *
 * So the server may say that an answer is being written which this page isn't reading:
 * another window asked for it, or this page did and was then reloaded, showed another
 * conversation, or lost its connection. The page reads along with it, from its first
 * event, as with an answer it asked for itself. It tries that once for each turn. When
 * the try fails, the page looks every so often until the answer is done.
 *
 * What answers cost is the server's to count too. A question refused at a usage limit
 * is handed to `usage`, which holds the limit until it starts over: nothing is sent
 * meanwhile, unless a model that costs nothing was chosen to carry on with.
 */
export function Chat({
  id,
  stored,
  model,
  offers,
  usage,
  onExists,
  onActivity,
  onStartOver,
}: {
  /** The conversation's id. For a new one the page chose it, and the first question
   * creates the conversation under it. */
  id: string;
  /** What the server has of it, or nothing for a new one. Read once. */
  stored: Conversation | null;
  /** The model that answers, once known: the installation's default one, or the one
   * that was chosen to carry on with at a usage limit. */
  model: Model | null;
  /** The models that cost nothing, to offer when a usage limit is reached. */
  offers: Model[];
  /** The usage limits of whoever has the session. */
  usage: UsageLimits;
  /** Called once the server has the conversation, so it has an address. */
  onExists: () => void;
  /** Called when the server has taken a question, and when its turn has ended: the
   * moments at which the conversation's place in the list changes. */
  onActivity: () => void;
  /** Called when the session turned out to be gone, and the person wants a new one. */
  onStartOver: () => void;
}) {
  const [initial] = useState(() =>
    stored === null ? [] : fromStored(stored.messages),
  );
  // When the conversation ends with a question: the last turn that tried to answer
  // it, as the server reports it. Nothing once the library has read an answer: what
  // it read says how its turn stands, until the server is asked again.
  const [open, setOpen] = useState<Turn | null>(stored?.unanswered ?? null);
  // The last turn this page tried to read along with, or is trying to.
  const [join, setJoin] = useState(() =>
    joinFor(stored?.unanswered ?? null, null),
  );
  const [exists, setExists] = useState(stored !== null);
  const [text, setText] = useState("");
  const [problem, setProblem] = useState<unknown>(null);
  // The error that broke off a reading after it had begun. The server had the turn
  // by then, and the page reads along with it again (`resync`): once it does, the
  // notice of the break is no longer true.
  const [brokeOff, setBrokeOff] = useState<unknown>(null);
  const [thinking, setThinking] = useStoredSwitch(THINKING_KEY);

  // The question last sent, until the server is known to have it: a refused
  // question's words go back into the box.
  const sent = useRef<{ id: string; text: string } | null>(null);
  // Counts what the person did. Something the server said about an earlier state is
  // dropped when it arrives after the next action.
  const actions = useRef(0);
  // Whether the person pressed Stop, and the turn is still to be stopped for it. The
  // library reports the same end of reading when this conversation leaves the page,
  // and then the answer is to go on.
  const stopping = useRef(false);
  // The try at reading along that the library is making, and whether it got to read
  // the turn's stream: then the library says how that ended, as for any answer.
  const trying = useRef<{ read: boolean } | null>(null);
  // What the library's last error put on the page, until the reading it ended is
  // over (`onFinish`).
  const reported = useRef<unknown>(null);
  const { scroller, content, away, follow } = useFollow();
  const box = useRef<HTMLTextAreaElement>(null);

  const transport = useMemo(() => turnTransport(id), [id]);
  const {
    messages,
    setMessages,
    sendMessage,
    regenerate,
    resumeStream,
    stop,
    status,
  } = useChat<ChatMessage>({
    id,
    messages: initial,
    generateId: newId,
    transport,
    onError: (error) => {
      // Reading along says nothing of what went wrong with it. The conversation,
      // read next, says what is wrong if anything is.
      if (trying.current !== null) {
        return;
      }
      // A refusal, or no connection. What a model's own failure was is part of the
      // turn, and shown with it. A refusal at a usage limit is not this question's
      // problem alone: it holds for every question until the limit starts over.
      if (
        (error instanceof ApiProblem || error instanceof TypeError) &&
        !usage.refused(error)
      ) {
        setProblem(error);
        reported.current = error;
      }
    },
    onData: (part) => {
      if (part.type === "data-usage-warning") {
        usage.pass(part.data);
      }
    },
    onFinish: ({ message, isAbort, isError }) => {
      if (trying.current !== null) {
        trying.current.read = true;
        // A join that got to read: the notice of the break goes (`brokeOff`).
        setProblem((current: unknown) =>
          current === brokeOff ? null : current,
        );
      }
      // A reading broke off after it had begun when the answer it built carries its
      // turn: the server had the question. One that never got that far, such as a
      // question that didn't reach the server, keeps its notice.
      const broke =
        isError && turnOf(message) !== null ? reported.current : null;
      reported.current = null;
      setBrokeOff(broke);
      // What was read says how the turn stands now, until the server is asked
      // again: an answer that was read along with too, which the server had said
      // was being written.
      setOpen(null);
      onActivity();
      // However the turn ended, what it cost is the server's to say. So is what is
      // left after a question that was refused.
      usage.refresh();
      if (isAbort) {
        // The reading ended here. The answer ends only if the person stopped it:
        // when the conversation left the page instead, it is written on.
        if (stopping.current) {
          stopping.current = false;
          void resync({ stop: true });
        }
      } else if (isError) {
        void resync({ stop: false });
      }
    },
  });
  const answering = status === "submitted" || status === "streaming";

  // Reading along again shows that the server is there, and has the turn: the
  // notice of the reading that broke off is no longer true. Any other notice stays.
  // It goes once the library reads the join's stream; at the latest when that
  // reading ends, for a stream read whole before the page is drawn again.
  if (answering && join !== null && !join.over && brokeOff !== null) {
    if (problem === brokeOff) {
      setProblem(null);
    }
    setBrokeOff(null);
  }

  // The first piece of an answer says that the server has the question, and with a
  // first question the conversation.
  const arriving = status === "streaming";
  useEffect(() => {
    if (arriving) {
      onActivity();
    }
  }, [arriving, onActivity]);

  const refreshUsage = usage.refresh;
  /** Ask the server what it has of this conversation, and show that. With `stop`, an
   * answer that is still being written is stopped first: the person asked for it.
   * An answer that is being written after all that is read along with, unless that
   * was tried already. Says whether one is being written, or nothing when the server
   * couldn't be asked, or its answer came too late to matter. */
  const resync = useCallback(
    async ({ stop }: { stop: boolean }): Promise<boolean | undefined> => {
      const action = actions.current;
      let conversation: Conversation | null;
      try {
        conversation = await readConversation(id);
      } catch (error) {
        setProblem(error);
        return undefined;
      }
      const writing = conversation?.unanswered;
      if (stop && writing?.status === "running") {
        try {
          // The API answers once the stop is recorded: what the conversation says
          // after that is how the answer ended, and what was kept of it.
          await cancelTurn(id, writing.id);
          conversation = await readConversation(id);
          // A stopped answer is counted for what it wrote, and is the conversation's
          // newest message now.
          refreshUsage();
          onActivity();
        } catch (error) {
          // It couldn't be stopped, so it is written on. The page says both: what
          // went wrong, and below, that an answer is still on its way.
          setProblem(error);
        }
      }
      if (action !== actions.current) {
        return undefined;
      }
      const last = sent.current;
      sent.current = null;
      setMessages(
        conversation === null ? [] : fromStored(conversation.messages),
      );
      const unanswered = conversation?.unanswered ?? null;
      setOpen(unanswered);
      setJoin((tried) => joinFor(unanswered, tried));
      if (conversation !== null) {
        setExists(true);
      }
      const arrived =
        last === null ||
        (conversation?.messages.some((message) => message.id === last.id) ??
          false);
      if (!arrived) {
        setText((current) => (current === "" ? last.text : current));
      }
      return unanswered?.status === "running";
    },
    [id, setMessages, refreshUsage, onActivity],
  );

  function send() {
    const words = text.trim();
    if (words === "") {
      return;
    }
    const question = {
      id: newId(),
      role: "user" as const,
      parts: [{ type: "text" as const, text: words }],
    };
    sent.current = { id: question.id, text: words };
    actions.current++;
    follow();
    setText("");
    setProblem(null);
    setOpen(null);
    usage.dismiss(); // a warning was read, or not: the next one is the API's to send
    // The new question follows the last message the server has. Two kinds of message
    // at the end of the page are left out on the way there:
    //
    // - an answer of which nothing arrived. Its stream began, so the page holds it,
    //   but the server stores no empty answer. The page learns that when it reads the
    //   conversation again, and a question can be sent before then;
    // - a question that got no answer. The new one takes its place: both follow the
    //   same message, and the server keeps the old one as an earlier version.
    let before = messages;
    const answer = before.at(-1);
    if (answer?.role === "assistant" && !hasContent(answer)) {
      before = before.slice(0, -1);
    }
    if (before.at(-1)?.role === "user") {
      before = before.slice(0, -1);
    }
    if (before !== messages) {
      setMessages(before);
    }
    void sendMessage(question, { body: options() });
  }

  function retry() {
    actions.current++;
    follow();
    setProblem(null);
    setOpen(null);
    void regenerate({ body: options() });
  }

  // The model that costs nothing and answers while a usage limit is reached, once the
  // person chose it and the API still offers it.
  const standIn =
    usage.standIn !== null && model?.id === usage.standIn ? model : null;

  function options(): TurnOptions {
    const reasoning = thinking && model?.thinking === true;
    return standIn === null
      ? { thinking: reasoning }
      : { thinking: reasoning, model: standIn.id };
  }

  // Once the server has the conversation, it gets its address.
  const answered = messages.some((message) => message.role === "assistant");
  useEffect(() => {
    if (exists || answered) {
      onExists();
    }
  }, [exists, answered, onExists]);

  // An answer that is being written, as the server last said, and that the library
  // isn't reading.
  const unread = !answering && open?.status === "running" ? open : null;
  // While the page tries to read along with it, it shows what it shows right after a
  // question was sent. Stop comes once the library reads: before that there is
  // nothing of its own to stop.
  const joining = unread !== null && join?.turn === unread.id && !join.over;
  // The try is over, and the server still says it is being written: look again
  // until it is over.
  const elsewhere = unread !== null && !joining;

  // Read along with it: the turn's stream, from the answer's first event, which the
  // library takes for the whole answer. However that ends, the library says so as
  // for an answer this page asked for (`onFinish`), unless it read nothing.
  const asked = join !== null && !join.over ? join : null;
  const readNothing = useEffectEvent(async () => {
    // The turn had ended meanwhile (204), or the stream couldn't be asked for. The
    // server says how it stands. Ended: what it cost, and where its conversation
    // stands in the list, were not read since either.
    const writing = await resync({ stop: false });
    if (writing === false) {
      refreshUsage();
      onActivity();
    }
  });
  useEffect(() => {
    if (asked === null) {
      return;
    }
    const attempt = { read: false };
    trying.current = attempt;
    let shown = true;
    const joined: JoinOptions = { turn: asked.turn };
    void resumeStream({ metadata: joined }).then(async () => {
      if (trying.current === attempt) {
        trying.current = null;
      }
      if (!shown) {
        // The conversation left the page, or React ran this effect twice on
        // purpose (StrictMode). The library dropped the request, and nothing came
        // of it: this try doesn't count.
        return;
      }
      if (!attempt.read) {
        await readNothing();
      }
      setJoin((tried) => (tried === asked ? { ...asked, over: true } : tried));
    });
    return () => {
      shown = false;
    };
  }, [asked, resumeStream]);

  useEffect(() => {
    if (!elsewhere) {
      return;
    }
    const timer = setTimeout(() => {
      void resync({ stop: false }).then((writing) => {
        if (writing === false) {
          // It ended without this page reading it: what it cost, and where its
          // conversation stands in the list, were not read since either.
          refreshUsage();
          onActivity();
        }
      });
    }, ELSEWHERE_MS);
    return () => {
      clearTimeout(timer);
    };
  }, [elsewhere, open, resync, refreshUsage, onActivity]);

  const last = messages.at(-1);
  const turn = last?.role === "assistant" ? turnOf(last) : open;
  const outcome = answering || turn === null ? null : outcomeOf(turn);
  const unready = model === null ? null : notReady(model);
  const blocked = isSessionEnd(problem)
    ? "Your session has ended."
    : elsewhere || joining
      ? "An answer is being written."
      : usage.reached !== null && standIn === null
        ? "You’ve reached your usage limit."
        : unready !== null
          ? "The model can't answer right now."
          : null;

  return (
    <>
      <div className="relative flex min-h-0 flex-1 flex-col">
        <div ref={scroller} className="min-h-0 flex-1 overflow-y-auto">
          <div
            ref={content}
            role="log"
            aria-label="Conversation"
            aria-busy={answering || joining}
            className="mx-auto flex w-full max-w-3xl flex-col gap-6 px-4 py-6"
          >
            {messages.length === 0 && (
              <div className="py-16 text-center">
                <p className="text-2xl font-semibold tracking-tight">
                  What would you like to know?
                </p>
                {model !== null && (
                  <p className="mt-2 text-sm text-muted-foreground">
                    Answered by {model.title}, a model running on this
                    installation.
                  </p>
                )}
              </div>
            )}
            {messages.map((message, index) => (
              <Message
                key={message.id}
                message={message}
                writing={answering && index === messages.length - 1}
              />
            ))}
            {(answering || joining) && last?.role === "user" && <Waiting />}
            {elsewhere && (
              <Notice>
                An answer to this is still being written. It appears here when
                it is done.
              </Notice>
            )}
            {outcome !== null && turn !== null && (
              <Notice
                actions={
                  canRetry(turn) &&
                  blocked === null && (
                    <NoticeButton onClick={retry}>Try again</NoticeButton>
                  )
                }
              >
                {outcome}
              </Notice>
            )}
          </div>
        </div>
        {away && (
          <button
            type="button"
            onClick={follow}
            aria-label="Go to the newest message"
            title="Go to the newest message"
            className="absolute bottom-3 left-1/2 flex size-9 -translate-x-1/2 items-center justify-center rounded-full border border-border bg-background text-lg shadow-md hover:bg-muted focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
          >
            <span aria-hidden="true">↓</span>
          </button>
        )}
      </div>
      <div className="mx-auto w-full max-w-3xl space-y-3 px-4 pb-4">
        {problem !== null && (
          <Notice
            tone="warning"
            actions={
              isSessionEnd(problem) ? (
                <NoticeButton onClick={onStartOver}>
                  Start a new session
                </NoticeButton>
              ) : (
                <NoticeButton
                  onClick={() => {
                    setProblem(null);
                  }}
                >
                  Dismiss
                </NoticeButton>
              )
            }
          >
            {explain(problem)}
          </Notice>
        )}
        {usage.reached !== null && (
          <LimitReached
            reached={usage.reached}
            standIn={standIn}
            offers={offers}
            onCarryOn={(free) => {
              usage.carryOnWith(free.id);
              // The question that was refused is back in the box: the next thing to
              // do is to send it.
              box.current?.focus();
            }}
          />
        )}
        <UsagePassed
          passed={usage.passed}
          read={usage.read}
          onDismiss={() => {
            usage.dismiss();
            box.current?.focus();
          }}
        />
        {unready !== null && (
          <Notice tone="warning">
            <p className="font-medium">{unready.title}</p>
            <p>
              {unready.advice}{" "}
              {unready.command !== null && (
                <code className="rounded bg-current/10 px-1.5 py-0.5">
                  {unready.command}
                </code>
              )}
            </p>
            <p>
              <a
                href={SETUP_GUIDE}
                target="_blank"
                rel="noreferrer"
                className="underline"
              >
                How to set up a model
              </a>
            </p>
          </Notice>
        )}
        <Composer
          box={box}
          text={text}
          onText={setText}
          onSend={send}
          onStop={() => {
            // The page stops reading at once. When the library has said so, the turn
            // is stopped at the server (`onFinish`, `resync`).
            stopping.current = true;
            void stop().finally(() => {
              // If the answer had just ended by itself, nothing was there to stop,
              // and nobody took the note. It must not wait for the next answer: that
              // one would be stopped by merely leaving its conversation.
              stopping.current = false;
            });
          }}
          answering={answering}
          blocked={blocked}
          thinking={
            model?.thinking === true ? { on: thinking, set: setThinking } : null
          }
        />
      </div>
    </>
  );
}
