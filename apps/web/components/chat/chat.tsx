"use client";

import { useChat } from "@ai-sdk/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Notice, NoticeButton } from "@/components/notice";
import { ApiProblem, getConversation } from "@/lib/api/client";
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
import type { TurnOptions } from "@/lib/chat/turns";
import { newId } from "@/lib/ids";

import { Composer } from "./composer";
import { useFollow } from "./follow";
import { Message, Waiting } from "./message";

const SETUP_GUIDE = "https://fatihrime.github.io/actio/getting-started/#models";
const THINKING_KEY = "actio.thinking";

/** How long the server may take to record that an answer was stopped, before the
 * page asks it again. */
const SETTLE_MS = 300;
const SETTLE_TRIES = 10;
/** How often to look whether an answer being written elsewhere is done. */
const ELSEWHERE_MS = 1500;

/** One conversation: its messages, and the box to continue it.
 *
 * The messages on the page are the chat library's (`useChat`): it streams an answer
 * into them as it arrives. The server keeps the conversation itself, so when a turn
 * ends in any way but well (stopped, failed, refused, connection lost), the page asks
 * the server what it has and shows that, instead of guessing.
 */
export function Chat({
  id,
  stored,
  model,
  onExists,
  onStartOver,
}: {
  /** The conversation's id. For a new one the page chose it, and the first question
   * creates the conversation under it. */
  id: string;
  /** What the server has of it, or nothing for a new one. Read once. */
  stored: Conversation | null;
  /** The model that answers, once known. */
  model: Model | null;
  /** Called once the server has the conversation, so it has an address. */
  onExists: () => void;
  /** Called when the session turned out to be gone, and the person wants a new one. */
  onStartOver: () => void;
}) {
  const [initial] = useState(() =>
    stored === null ? [] : fromStored(stored.messages),
  );
  // When the conversation ends with a question: the last turn that tried to answer
  // it, as the server reports it.
  const [open, setOpen] = useState<Turn | null>(stored?.unanswered ?? null);
  const [exists, setExists] = useState(stored !== null);
  const [text, setText] = useState("");
  const [problem, setProblem] = useState<unknown>(null);
  const [thinking, setThinking] = useStoredSwitch(THINKING_KEY);

  // The question last sent, until the server is known to have it: a refused
  // question's words go back into the box.
  const sent = useRef<{ id: string; text: string } | null>(null);
  // Counts what the person did. Something the server said about an earlier state is
  // dropped when it arrives after the next action.
  const actions = useRef(0);
  const { scroller, content, away, follow } = useFollow();

  const transport = useMemo(() => turnTransport(id), [id]);
  const { messages, setMessages, sendMessage, regenerate, stop, status } =
    useChat<ChatMessage>({
      id,
      messages: initial,
      generateId: newId,
      transport,
      onError: (error) => {
        // A refusal, or no connection. What a model's own failure was is part of the
        // turn, and shown with it.
        if (error instanceof ApiProblem || error instanceof TypeError) {
          setProblem(error);
        }
      },
      onFinish: ({ isAbort, isError }) => {
        if (isAbort || isError) {
          void resync({ settle: isAbort });
        }
      },
    });
  const answering = status === "submitted" || status === "streaming";

  /** Ask the server what it has of this conversation, and show that. */
  const resync = useCallback(
    async ({ settle }: { settle: boolean }) => {
      const action = actions.current;
      for (let attempt = 1; ; attempt++) {
        let conversation: Conversation | null;
        try {
          conversation = await getConversation(id);
        } catch (error) {
          if (!(error instanceof ApiProblem && error.status === 404)) {
            setProblem(error);
            return;
          }
          conversation = null; // its first question never arrived
        }
        if (action !== actions.current) {
          return;
        }
        // Just after a stop, the server may not have recorded it yet.
        const recording =
          settle &&
          conversation?.unanswered?.status === "running" &&
          attempt < SETTLE_TRIES;
        if (!recording) {
          const last = sent.current;
          sent.current = null;
          setMessages(
            conversation === null ? [] : fromStored(conversation.messages),
          );
          setOpen(conversation?.unanswered ?? null);
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
          return;
        }
        await new Promise((resolve) => setTimeout(resolve, SETTLE_MS));
      }
    },
    [id, setMessages],
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

  function options(): TurnOptions {
    return { thinking: thinking && model?.thinking === true };
  }

  // Once the server has the conversation, it gets its address.
  const answered = messages.some((message) => message.role === "assistant");
  useEffect(() => {
    if (exists || answered) {
      onExists();
    }
  }, [exists, answered, onExists]);

  // An answer that another window started can't be followed from here yet. Look
  // again until it is done.
  const elsewhere = !answering && open?.status === "running";
  useEffect(() => {
    if (!elsewhere) {
      return;
    }
    const timer = setTimeout(
      () => void resync({ settle: false }),
      ELSEWHERE_MS,
    );
    return () => {
      clearTimeout(timer);
    };
  }, [elsewhere, open, resync]);

  const last = messages.at(-1);
  const turn = last?.role === "assistant" ? turnOf(last) : open;
  const outcome = answering || turn === null ? null : outcomeOf(turn);
  const unready = model === null ? null : notReady(model);
  const blocked = isSessionEnd(problem)
    ? "Your session has ended."
    : elsewhere
      ? "An answer is being written."
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
            aria-busy={answering}
            className="mx-auto flex w-full max-w-3xl flex-col gap-6 px-4 py-6"
          >
            {messages.length === 0 && (
              <div className="py-16 text-center">
                <p className="text-2xl font-semibold tracking-tight">
                  What would you like to know?
                </p>
                {model !== null && (
                  <p className="mt-2 text-sm text-zinc-500">
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
            {answering && last?.role === "user" && <Waiting />}
            {elsewhere && (
              <Notice>
                An answer to this is being written in another window. It appears
                here when it is done.
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
            className="absolute bottom-3 left-1/2 flex size-9 -translate-x-1/2 items-center justify-center rounded-full border border-border bg-background text-lg shadow-md hover:bg-muted focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-zinc-500"
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
        {unready !== null && (
          <Notice tone="warning">
            <p className="font-medium">{unready.title}</p>
            <p>
              {unready.advice}{" "}
              {unready.command !== null && (
                <code className="rounded bg-black/10 px-1.5 py-0.5 dark:bg-white/10">
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
          text={text}
          onText={setText}
          onSend={send}
          onStop={() => void stop()}
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

/** A switch that is remembered in this browser. */
function useStoredSwitch(key: string): [boolean, (on: boolean) => void] {
  const [on, setOn] = useState(() => {
    try {
      return window.localStorage.getItem(key) === "on";
    } catch {
      return false; // storage is off limits, as in some private windows
    }
  });
  const set = useCallback(
    (next: boolean) => {
      setOn(next);
      try {
        window.localStorage.setItem(key, next ? "on" : "off");
      } catch {
        // Not remembered, then.
      }
    },
    [key],
  );
  return [on, set];
}
