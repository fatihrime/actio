"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import useSWR, { useSWRConfig } from "swr";

import { PanelIcon, PlusIcon } from "@/components/icons";
import { Notice, NoticeButton } from "@/components/notice";
import { SessionGate, useSession } from "@/components/session-gate";
import { Sidebar } from "@/components/sidebar/sidebar";
import { ApiProblem, getConversation, listModels } from "@/lib/api/client";
import type { Conversation, Model, ModelList } from "@/lib/api/client";
import { useConversations } from "@/lib/chat/list";
import { explain } from "@/lib/chat/outcomes";
import { newId } from "@/lib/ids";
import { conversationIn, conversationPath, go, usePath } from "@/lib/location";
import { useMedia, WIDE } from "@/lib/media";
import { useStoredSwitch } from "@/lib/stored-switch";

import { Chat } from "./chat";

/** How often to ask again while the model can't answer, to notice when it can. */
const UNREADY_POLL_MS = 5000;
const LIST_HIDDEN_KEY = "actio.conversations.hidden";

const HEADER_BUTTON =
  "flex items-center gap-1.5 rounded-lg border border-input px-2.5 py-1.5 text-sm font-medium hover:bg-muted focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring";

/** The app's one screen: the list of conversations, and beside it whichever
 * conversation the address names, or a new one.
 *
 * - At "/" it is a new conversation, under an id made here. Nothing is stored until
 *   its first question is sent; then the address becomes "/c/<id>".
 * - At "/c/<id>" the conversation is loaded from the API, unless this page started it
 *   and has shown it ever since.
 *
 * Both of the app's pages show this one component, so moving between conversations
 * never loads a page.
 */
export function ChatApp() {
  const inAddress = conversationIn(usePath());
  // The id the next new conversation gets.
  const [fresh, setFresh] = useState(newId);
  // A conversation that began here is on the page for as long as the page shows it.
  // Once the address has named another one, what was held of it is gone, and coming
  // back means reading it from the server like any other. So from then on a new
  // conversation gets another id.
  const [lastShown, setLastShown] = useState(inAddress);
  if (lastShown !== inAddress) {
    setLastShown(inAddress);
    if (inAddress !== null && inAddress !== fresh) {
      setFresh(newId());
    }
  }
  const id = inAddress ?? fresh;

  const me = useSession().data;
  const list = useConversations(me?.id ?? null);

  // In a wide window the list stands beside the chat, unless it was put away. In a
  // narrow one it lies over the chat while it is wanted, and is away otherwise.
  const wide = useMedia(WIDE, true);
  const [putAway, setPutAway] = useStoredSwitch(LIST_HIDDEN_KEY);
  const [over, setOver] = useState(false);
  const listShown = wide ? !putAway : over;
  const listButton = useRef<HTMLButtonElement>(null);

  const onExists = useCallback(() => {
    go(conversationPath(id), { replace: true });
  }, [id]);

  function startNew({ replace = false } = {}) {
    setFresh(newId());
    setOver(false);
    go("/", { replace });
  }

  // A guest whose session ended can't have it back, nor the conversations that went
  // with it. A new session starts with a new conversation.
  const { mutate } = useSWRConfig();
  function startOver() {
    void mutate("session");
    startNew();
  }

  return (
    <div
      className="flex h-dvh flex-col"
      onKeyDown={(event) => {
        // Escape puts away the list that lies over the chat, wherever the keyboard
        // is. Whatever has a use of its own for the key keeps it to itself: a title
        // being typed, a question before deleting.
        if (event.key === "Escape" && over && !wide) {
          setOver(false);
          listButton.current?.focus();
        }
      }}
    >
      <header className="flex shrink-0 items-center gap-3 border-b border-border px-3 py-2">
        {me !== undefined && (
          <button
            ref={listButton}
            type="button"
            onClick={() => {
              if (wide) {
                setPutAway(!putAway);
              } else {
                setOver(!over);
              }
            }}
            aria-expanded={listShown}
            aria-label="Conversations"
            title={
              listShown ? "Hide the conversations" : "Show the conversations"
            }
            className={HEADER_BUTTON}
          >
            <PanelIcon />
          </button>
        )}
        <h1 className="text-lg font-semibold tracking-tight">Actio</h1>
        {me !== undefined && (
          <button
            type="button"
            onClick={() => {
              startNew();
            }}
            className={`ml-auto ${HEADER_BUTTON}`}
          >
            <PlusIcon />
            New chat
          </button>
        )}
      </header>
      <SessionGate>
        <div className="relative flex min-h-0 flex-1">
          {listShown && !wide && (
            // Over a narrow window's chat, the rest of the screen closes the list.
            <button
              type="button"
              aria-label="Close the conversations"
              tabIndex={-1}
              onClick={() => {
                setOver(false);
              }}
              className="absolute inset-0 z-10 cursor-default bg-black/40"
            />
          )}
          {listShown && (
            <aside
              className={`flex w-72 shrink-0 flex-col border-r border-border bg-sidebar ${
                wide
                  ? ""
                  : "absolute inset-y-0 left-0 z-20 max-w-[85%] shadow-xl"
              }`}
            >
              <Sidebar
                list={list}
                current={inAddress}
                // Over the chat it was just asked for: the keyboard goes there.
                takesFocus={!wide}
                onOpen={(conversation) => {
                  setOver(false);
                  go(conversationPath(conversation));
                }}
                onDeleted={(conversation) => {
                  if (conversation === id) {
                    // Nothing is left to show, or to go back to.
                    startNew({ replace: true });
                  }
                }}
              />
            </aside>
          )}
          <main className="flex min-w-0 flex-1 flex-col">
            <Current
              id={id}
              // A conversation this page started, and still shows, is already on it:
              // nothing to load.
              load={inAddress !== null && inAddress !== fresh}
              onExists={onExists}
              onActivity={list.refresh}
              onStartNew={startNew}
              onStartOver={startOver}
            />
          </main>
        </div>
      </SessionGate>
    </div>
  );
}

/** The conversation that is shown, once there is a session to ask the API with. */
function Current({
  id,
  load,
  onExists,
  onActivity,
  onStartNew,
  onStartOver,
}: {
  id: string;
  load: boolean;
  onExists: () => void;
  onActivity: () => void;
  onStartNew: () => void;
  onStartOver: () => void;
}) {
  const model = useDefaultModel();
  const chat = { id, model, onExists, onActivity, onStartOver };
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
  onActivity,
  onStartNew,
  onStartOver,
}: {
  id: string;
  model: Model | null;
  onExists: () => void;
  onActivity: () => void;
  onStartNew: () => void;
  onStartOver: () => void;
}) {
  const loaded = useStored(id);

  if (loaded.state === "loading") {
    return (
      <p
        className="px-4 py-10 text-center text-sm text-muted-foreground"
        role="status"
      >
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
      onActivity={onActivity}
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
