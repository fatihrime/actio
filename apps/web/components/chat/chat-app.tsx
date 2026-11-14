"use client";

import { useCallback, useEffect, useState } from "react";
import useSWR, { useSWRConfig } from "swr";

import { Notice, NoticeButton } from "@/components/notice";
import { SessionGate } from "@/components/session-gate";
import { ApiProblem, getConversation, listModels } from "@/lib/api/client";
import type { Conversation, Model, ModelList } from "@/lib/api/client";
import { explain } from "@/lib/chat/outcomes";
import { newId } from "@/lib/ids";
import { conversationIn, conversationPath, go, usePath } from "@/lib/location";

import { Chat } from "./chat";

/** How often to ask again while the model can't answer, to notice when it can. */
const UNREADY_POLL_MS = 5000;

/** The chat: whichever conversation the address names, or a new one.
 *
 * - At "/" it is a new conversation, under an id made here. Nothing is stored until
 *   its first question is sent; then the address becomes "/c/<id>".
 * - At "/c/<id>" the conversation is loaded from the API, unless this page started it.
 *
 * Both of the app's pages show this one component, so moving between conversations
 * never loads a page.
 */
export function ChatApp() {
  const inAddress = conversationIn(usePath());
  // The id the next new conversation gets.
  const [fresh, setFresh] = useState(newId);
  const id = inAddress ?? fresh;

  const onExists = useCallback(() => {
    go(conversationPath(id), { replace: true });
  }, [id]);

  function startNew() {
    setFresh(newId());
    go("/");
  }

  // A guest whose session ended can't have it back, nor the conversations that went
  // with it. A new session starts with a new conversation.
  const { mutate } = useSWRConfig();
  function startOver() {
    void mutate("session");
    startNew();
  }

  return (
    <div className="flex h-dvh flex-col">
      <header className="flex shrink-0 items-center justify-between border-b border-zinc-200 px-4 py-2.5 dark:border-zinc-800">
        <h1 className="text-lg font-semibold tracking-tight">Actio</h1>
        <button
          type="button"
          onClick={startNew}
          className="rounded-lg border border-zinc-300 px-3 py-1.5 text-sm font-medium hover:bg-zinc-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-zinc-500 dark:border-zinc-700 dark:hover:bg-zinc-800"
        >
          New chat
        </button>
      </header>
      <SessionGate>
        <Current
          id={id}
          // A conversation this page started is already on it: nothing to load.
          load={inAddress !== null && inAddress !== fresh}
          onExists={onExists}
          onStartNew={startNew}
          onStartOver={startOver}
        />
      </SessionGate>
    </div>
  );
}

/** The conversation that is shown, once there is a session to ask the API with. */
function Current({
  id,
  load,
  onExists,
  onStartNew,
  onStartOver,
}: {
  id: string;
  load: boolean;
  onExists: () => void;
  onStartNew: () => void;
  onStartOver: () => void;
}) {
  const model = useDefaultModel();
  const chat = { id, model, onExists, onStartOver };
  // Keyed by the id: a conversation that is opened again is loaded again, and never
  // shown as it was the last time it was on the page.
  return load ? (
    <StoredChat key={id} {...chat} onStartNew={onStartNew} />
  ) : (
    <Chat key={id} {...chat} stored={null} />
  );
}

/** A conversation the API has: loaded first, then shown. */
function StoredChat({
  id,
  model,
  onExists,
  onStartNew,
  onStartOver,
}: {
  id: string;
  model: Model | null;
  onExists: () => void;
  onStartNew: () => void;
  onStartOver: () => void;
}) {
  const loaded = useStored(id);

  if (loaded.state === "loading") {
    return (
      <p className="px-4 py-10 text-center text-sm text-zinc-500" role="status">
        Loading the conversation…
      </p>
    );
  }
  if (loaded.state === "failed") {
    return (
      <div className="mx-auto w-full max-w-3xl px-4 py-10">
        <Notice
          tone="warning"
          actions={
            loaded.missing ? (
              <NoticeButton onClick={onStartNew}>Start a new chat</NoticeButton>
            ) : (
              <NoticeButton onClick={loaded.retry}>Try again</NoticeButton>
            )
          }
        >
          {loaded.missing
            ? "This conversation doesn’t exist, or belongs to another session."
            : explain(loaded.error)}
        </Notice>
      </div>
    );
  }
  return (
    <Chat
      id={id}
      stored={loaded.conversation}
      model={model}
      onExists={onExists}
      onStartOver={onStartOver}
    />
  );
}

type Stored =
  | { state: "loading" }
  | { state: "ready"; conversation: Conversation }
  | { state: "failed"; missing: boolean; error: unknown; retry: () => void };

/** The conversation `id` as the API has it now. It is asked for once, when the
 * component that uses this appears, and again for each `retry`. */
function useStored(id: string): Stored {
  const [result, setResult] = useState<{
    attempt: number;
    conversation?: Conversation;
    error?: unknown;
  } | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let current = true;
    getConversation(id).then(
      (conversation) => {
        if (current) {
          setResult({ attempt, conversation });
        }
      },
      (error: unknown) => {
        if (current) {
          setResult({ attempt, error });
        }
      },
    );
    return () => {
      current = false;
    };
  }, [id, attempt]);

  if (result?.attempt !== attempt) {
    return { state: "loading" }; // nothing yet, or what is held is an earlier try's
  }
  if (result.conversation !== undefined) {
    return { state: "ready", conversation: result.conversation };
  }
  return {
    state: "failed",
    missing: result.error instanceof ApiProblem && result.error.status === 404,
    error: result.error,
    retry: () => {
      setAttempt(attempt + 1);
    },
  };
}

/** The model that answers when a request names none, with whether it can right now.
 * Nothing until the API has said. */
function useDefaultModel(): Model | null {
  const { data } = useSWR("models", listModels, {
    refreshInterval: (latest: ModelList | undefined) =>
      latest !== undefined && defaultOf(latest)?.status !== "ready"
        ? UNREADY_POLL_MS
        : 0,
  });
  return data === undefined ? null : defaultOf(data);
}

function defaultOf(list: ModelList): Model | null {
  return list.models.find((model) => model.id === list.default) ?? null;
}
