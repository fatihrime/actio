"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import useSWR from "swr";

import { SessionPlace } from "@/components/account/session-place";
import { PanelIcon, PersonIcon, PlusIcon } from "@/components/icons";
import { InAppLink } from "@/components/in-app-link";
import { Notice, NoticeButton } from "@/components/notice";
import { SessionStatus } from "@/components/session-status";
import { Sidebar } from "@/components/sidebar/sidebar";
import { ApiProblem, getConversation, listModels } from "@/lib/api/client";
import type { Conversation, Me, Model, ModelList } from "@/lib/api/client";
import { useConversations } from "@/lib/chat/list";
import { explain } from "@/lib/chat/outcomes";
import { newId } from "@/lib/ids";
import {
  ACCOUNT,
  conversationPath,
  go,
  LOGIN,
  placeIn,
  SIGNUP,
  usePath,
} from "@/lib/location";
import type { Place } from "@/lib/location";
import { useMedia, WIDE } from "@/lib/media";
import { useSession } from "@/lib/session";
import { useStoredSwitch } from "@/lib/stored-switch";

import { Chat } from "./chat";

/** How often to ask again while the model can't answer, to notice when it can. */
const UNREADY_POLL_MS = 5000;
const LIST_HIDDEN_KEY = "actio.conversations.hidden";

const HEADER_BUTTON =
  "flex items-center gap-1.5 rounded-lg border border-input px-2.5 py-1.5 text-sm font-medium hover:bg-muted focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring";

/** What the browser's tab says at each place. */
const TITLES: Record<Place["kind"], string> = {
  chat: "Actio",
  login: "Log in · Actio",
  signup: "Sign up · Actio",
  account: "Your account · Actio",
};

/** The app's one screen: the list of conversations, and beside it what the address
 * names.
 *
 * - At "/" that is a new conversation, under an id made here. Nothing is stored until
 *   its first question is sent; then the address becomes "/c/<id>".
 * - At "/c/<id>" the conversation is loaded from the API, unless this page started it
 *   and has shown it ever since.
 * - At "/login", "/signup" and "/account" it is the form for that (ADR 0019).
 *
 * Every page of the app shows this one component, so moving between them never loads
 * a page.
 */
export function ChatApp() {
  const path = usePath();
  const place = placeIn(path);
  const inAddress = place.kind === "chat" ? place.conversation : null;
  // The id the next new conversation gets.
  const [fresh, setFresh] = useState(newId);
  // A conversation that began here is on the page for as long as the page shows it.
  // Once the address has named anything else, another conversation or a form, what
  // was held of it is gone, and coming back means reading it from the server like
  // any other. So from then on a new conversation gets another id.
  const showsItsOwn =
    place.kind === "chat" && (inAddress === null || inAddress === fresh);
  const [lastPath, setLastPath] = useState(path);
  // Where the chat was left for one of the forms: where to go back to afterwards.
  const [leftAt, setLeftAt] = useState<string | null>(null);
  if (lastPath !== path) {
    setLastPath(path);
    if (!showsItsOwn) {
      setFresh(newId());
    }
    if (place.kind !== "chat" && placeIn(lastPath).kind === "chat") {
      setLeftAt(lastPath);
    }
  }
  const id = inAddress ?? fresh;

  const session = useSession();
  const { me } = session;
  const list = useConversations(session.holder);

  useEffect(() => {
    document.title = TITLES[place.kind];
  }, [place.kind]);

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

  // A session that has ended can't be had back: a guest's is gone with the guest, and
  // an account's needs its password again. A new session starts as a guest, with a
  // new conversation, and nothing of the one before.
  function startOver() {
    session.startOver();
    startNew({ replace: true });
  }

  // After signing up or logging in: back to where the chat was left. A guest's
  // conversations went with them, so the one that was on the page is still theirs.
  function enter(as: Me) {
    session.become(as);
    go(leftAt ?? "/", { replace: true });
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
        <div className="ml-auto flex items-center gap-2">
          <Who
            me={me}
            // Someone whose guest session couldn't be started may have an account.
            canLogIn={session.error !== null}
            at={place.kind}
            onFollow={() => {
              setOver(false);
            }}
          />
          {me !== undefined && (
            <button
              type="button"
              onClick={() => {
                startNew();
              }}
              className={HEADER_BUTTON}
            >
              <PlusIcon />
              New chat
            </button>
          )}
        </div>
      </header>
      <div className="relative flex min-h-0 flex-1">
        {me !== undefined && (
          <>
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
                  guest={me.is_guest}
                  onChosen={() => {
                    setOver(false);
                  }}
                  onDeleted={(conversation) => {
                    if (conversation === id && place.kind === "chat") {
                      // Nothing is left to show, or to go back to.
                      startNew({ replace: true });
                    }
                  }}
                />
              </aside>
            )}
          </>
        )}
        <main className="flex min-w-0 flex-1 flex-col">
          {place.kind !== "chat" ? (
            <SessionPlace
              place={place.kind}
              session={session}
              hasConversations={(list.items?.length ?? 0) > 0}
              onEntered={enter}
              onLoggedOut={startOver}
            />
          ) : me === undefined ? (
            <SessionStatus session={session} />
          ) : (
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
          )}
        </main>
      </div>
    </div>
  );
}

/** Whose session it is, in the header: where a guest logs in or signs up, and where
 * an account sees to itself. */
function Who({
  me,
  canLogIn,
  at,
  onFollow,
}: {
  me: Me | undefined;
  /** Whether to offer logging in although nobody has a session. */
  canLogIn: boolean;
  /** The place on the page: no link leads to where one already is. */
  at: Place["kind"];
  onFollow: () => void;
}) {
  if (me === undefined) {
    return canLogIn && at !== "login" ? (
      <InAppLink href={LOGIN} className={HEADER_BUTTON}>
        Log in
      </InAppLink>
    ) : null;
  }
  if (me.is_guest) {
    return (
      <>
        {at !== "signup" && (
          <InAppLink
            href={SIGNUP}
            onFollow={onFollow}
            // In a narrow window it gives way: the list and the login form lead to it.
            className={`max-sm:hidden ${HEADER_BUTTON}`}
          >
            Sign up
          </InAppLink>
        )}
        {at !== "login" && (
          <InAppLink href={LOGIN} onFollow={onFollow} className={HEADER_BUTTON}>
            Log in
          </InAppLink>
        )}
      </>
    );
  }
  return (
    <InAppLink
      href={ACCOUNT}
      onFollow={onFollow}
      aria-label={`Account: ${me.email ?? ""}`}
      title="Your account"
      className={HEADER_BUTTON}
    >
      <PersonIcon />
      <span className="max-w-48 truncate max-sm:hidden">{me.email}</span>
    </InAppLink>
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
