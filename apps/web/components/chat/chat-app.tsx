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
import { LIST_HIDDEN_KEY, markListAway } from "@/lib/frame";
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
import { useUsage } from "@/lib/usage";
import type { UsageLimits } from "@/lib/usage";

import { Chat } from "./chat";
import { fetchRenderer, useRenderer } from "./message";

/** How often to ask again while the model can't answer, to notice when it can. */
const UNREADY_POLL_MS = 5000;

/** How long a conversation that has been read waits for the code that shows its
 * answers as markdown, before it is shown as written, to become markdown once the code
 * is here (ADR 0016, revision of 2026-11-21). On a slow connection (1.6 Mbit/s) the
 * code takes 1.7 to 2.4 seconds: a shorter wait would show the marks there. A download
 * that stalls must not hide a conversation the page already has. */
export const RENDERER_WAIT_MS = 3000;

/** How often, at the most, a wait goes without looking at the clock. A test moves the
 * clock (tests/clock.ts), and the page notices when it next looks. */
const CLOCK_LOOK_MS = 1000;

const HEADER_BUTTON =
  "flex items-center gap-1.5 rounded-lg border border-input px-2.5 py-1.5 text-sm font-medium hover:bg-muted focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring";

/** The width, edge and colour of the list of conversations beside the chat, which its
 * place has too while nobody has a session yet: one changes with the other. */
const LIST_PLACE = "w-72 shrink-0 border-r border-border bg-sidebar";

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
  // Whether the page knows whose session it has. What arrives with the session fills
  // places that the page's frame held for it until then (ADR 0015, revision of
  // 2026-11-21).
  const known = me !== undefined;
  const list = useConversations(session.holder);
  const usage = useUsage(session.holder);

  useEffect(() => {
    document.title = TITLES[place.kind];
  }, [place.kind]);

  // The code that shows answers as markdown is fetched ahead, so that an answer is
  // never shown first as the marks it was written in. Not before the page knows whose
  // session it has: the first paint, and the request for the session, don't wait
  // behind it.
  useEffect(() => {
    if (known) {
      fetchRenderer();
    }
  }, [known]);

  // In a wide window the list stands beside the chat, unless it was put away. In a
  // narrow one it lies over the chat while it is wanted, and is away otherwise.
  const wide = useMedia(WIDE, true);
  const [putAway, setPutAway] = useStoredSwitch(LIST_HIDDEN_KEY);
  const [over, setOver] = useState(false);
  const listShown = wide ? !putAway : over;
  const listButton = useRef<HTMLButtonElement>(null);

  // The mark on the page's root element says what the switch says, from the page's
  // start on: the script in the head set it before the first paint (lib/frame.ts),
  // and the list's place, decided by it, is shown again after a log-out, with no page
  // loaded in between.
  useEffect(() => {
    markListAway(putAway);
  }, [putAway]);

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
      {/* The header's two buttons are there from the first paint, so that nothing
          moves when the session arrives. Until then they keep their places, unseen
          and out of reach, and carry nothing that depends on what only the browser
          knows. The remembered switch is read at the browser's first render, and
          React doesn't repair an attribute that differs from what the server sent;
          the window's width is read right after, and would change them in front of
          the reader. */}
      <header className="flex shrink-0 items-center gap-3 border-b border-border px-3 py-2">
        <button
          ref={listButton}
          type="button"
          disabled={!known}
          aria-hidden={known ? undefined : true}
          onClick={() => {
            if (wide) {
              setPutAway(!putAway);
            } else {
              setOver(!over);
            }
          }}
          aria-expanded={known ? listShown : undefined}
          aria-label="Conversations"
          title={
            known
              ? listShown
                ? "Hide the conversations"
                : "Show the conversations"
              : undefined
          }
          className={`${HEADER_BUTTON} ${known ? "" : "invisible"}`}
        >
          <PanelIcon />
        </button>
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
          <button
            type="button"
            disabled={!known}
            aria-hidden={known ? undefined : true}
            onClick={() => {
              startNew();
            }}
            className={`${HEADER_BUTTON} ${known ? "" : "invisible"}`}
          >
            <PlusIcon />
            New chat
          </button>
        </div>
      </header>
      <div className="relative flex min-h-0 flex-1">
        {me === undefined ? (
          // The list's place, for as long as nobody has a session: in a wide window,
          // unless the list was put away. CSS decides both, and the mark on the
          // page's root element (lib/frame.ts): the server can't know either. Not an
          // `aside`, which would be announced with nothing in it.
          <div
            aria-hidden
            className={`hidden md:block list-away:hidden ${LIST_PLACE}`}
          />
        ) : (
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
                className={`flex flex-col ${LIST_PLACE} ${
                  wide
                    ? ""
                    : "absolute inset-y-0 left-0 z-20 max-w-[85%] shadow-xl"
                }`}
              >
                <Sidebar
                  list={list}
                  usage={usage}
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
              holder={session.holder}
              usage={usage}
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
  holder,
  usage,
  onExists,
  onActivity,
  onStartNew,
  onStartOver,
}: {
  id: string;
  load: boolean;
  /** What the session's data is held under (`Session.holder`). */
  holder: string | null;
  usage: UsageLimits;
  onExists: () => void;
  onActivity: () => void;
  onStartNew: () => void;
  onStartOver: () => void;
}) {
  const models = useModels(holder, usage.standIn);
  const chat = {
    id,
    model: models === null ? null : answering(models, usage.standIn),
    // The models that cost nothing, as far as this installation still offers them.
    offers: (usage.reached?.free ?? []).flatMap(
      (free) => models?.models.find((model) => model.id === free) ?? [],
    ),
    usage,
    onExists,
    onActivity,
    onStartOver,
  };
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
  offers,
  usage,
  onExists,
  onActivity,
  onStartNew,
  onStartOver,
}: {
  id: string;
  model: Model | null;
  offers: Model[];
  usage: UsageLimits;
  onExists: () => void;
  onActivity: () => void;
  onStartNew: () => void;
  onStartOver: () => void;
}) {
  const loaded = useStored(id);
  // Its answers wait for the code that shows them as markdown, which is on its way by
  // now. Shown as written first, they would be swapped for markdown a moment later, and
  // the page would jump. But not for long: past the wait, they are shown as written.
  const renderer = useRenderer();
  const held =
    loaded.state === "ready" &&
    (renderer === null || renderer.state === "fetching");
  const heldTooLong = useWaitedOut(held, RENDERER_WAIT_MS);

  if (loaded.state === "loading" || (held && !heldTooLong)) {
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
      offers={offers}
      usage={usage}
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

/** Whether `waiting` has been true for `ms` milliseconds, by the clock. Once it has,
 * this stays true. */
function useWaitedOut(waiting: boolean, ms: number): boolean {
  const [over, setOver] = useState(false);
  useEffect(() => {
    if (!waiting) {
      return;
    }
    const until = Date.now() + ms;
    let timer: ReturnType<typeof setTimeout> | undefined;
    function look() {
      const left = until - Date.now();
      if (left <= 0) {
        setOver(true);
      } else {
        timer = setTimeout(look, Math.min(left, CLOCK_LOOK_MS));
      }
    }
    timer = setTimeout(look, Math.min(ms, CLOCK_LOOK_MS));
    return () => {
      clearTimeout(timer);
    };
  }, [waiting, ms]);
  return over;
}

/** The models on offer, with whether each can answer right now. Nothing until the API
 * has said. Asked again and again while the one that answers can't.
 *
 * Held under the session it was read in, like everything read from the API: a new
 * session reads it anew, instead of looking where the last one's answer was. */
function useModels(
  holder: string | null,
  standIn: string | null,
): ModelList | null {
  const { data } = useSWR(
    holder === null ? null : (["models", holder] as const),
    listModels,
    {
      refreshInterval: (latest: ModelList | undefined) =>
        latest !== undefined && answering(latest, standIn)?.status !== "ready"
          ? UNREADY_POLL_MS
          : 0,
    },
  );
  return data ?? null;
}

/** The model that answers: the one a request gets that names none, unless another was
 * chosen to stand in for it and is still on offer. */
function answering(list: ModelList, standIn: string | null): Model | null {
  const named = (id: string | null) =>
    list.models.find((model) => model.id === id);
  return named(standIn) ?? named(list.default) ?? null;
}
