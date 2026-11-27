"use client";

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import type { ReactElement } from "react";
import useSWR from "swr";

import { SessionPlace } from "@/components/account/session-place";
import { PanelIcon, PersonIcon, PlusIcon } from "@/components/icons";
import { InAppLink } from "@/components/in-app-link";
import { Notice, NoticeButton } from "@/components/notice";
import { SessionStatus } from "@/components/session-status";
import { Sidebar } from "@/components/sidebar/sidebar";
import { afterPaint } from "@/lib/after-paint";
import { ApiProblem, getConversation, listModels } from "@/lib/api/client";
import type { Conversation, Me, Model, ModelList } from "@/lib/api/client";
import { answering, useChosen } from "@/lib/chat/chosen";
import { useConversations } from "@/lib/chat/list";
import { explain } from "@/lib/chat/outcomes";
import { holdPlacesFor } from "@/lib/chat/places";
import { warmUp } from "@/lib/chat/warm-up";
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
import { followChoicesMadeElsewhere } from "@/lib/scheme";
import { useSession } from "@/lib/session";
import { useStoredSwitch } from "@/lib/stored-switch";
import { useUsage } from "@/lib/usage";
import type { UsageLimits } from "@/lib/usage";

import { Chat } from "./chat";
import { fetchRenderer, useWaitsForRenderer } from "./message";
import { ModelPicker } from "./model-picker";

// How long a conversation that has been read waits for the code that shows its answers
// as markdown, at the most: the page's one wait for that code, which its answers share
// (message.tsx, ADR 0016, revision "an answer that arrives waits for the renderer
// too"). It was this file's own until then, and is named here too.
export { RENDERER_WAIT_MS } from "./message";

/** How often to ask again while the model can't answer, to notice when it can. */
const UNREADY_POLL_MS = 5000;

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

  // The code that shows answers as markdown is fetched ahead, so that an answer finds it
  // here: until it is, an answer's words wait for it, and are never shown first as the
  // marks they were written in (message.tsx). Not before the page knows whose
  // session it has, nor before the list of conversations has been read, or couldn't
  // be, and the frame that shows it has been painted: the first paint, the request for
  // the session and the list's rows don't wait behind it. Its code is run as it
  // arrives, one task of some 115 ms at a quarter of the CPU's speed, which came
  // before the list's rows were painted (ADR 0016, revision of 2026-11-22). Whatever
  // needs it sooner asks for it at once itself: a question being written (chat.tsx),
  // an answer or a stored conversation on the page (`useRenderer`).
  const listRead = list.items !== null || list.error !== null;
  useEffect(() => {
    if (!known || !listRead) {
      return;
    }
    return afterPaint(fetchRenderer);
  }, [known, listRead]);

  // In a wide window the list stands beside the chat, unless it was put away. In a
  // narrow one it lies over the chat while it is wanted, and is away otherwise.
  const wide = useMedia(WIDE, true);
  const [putAway, setPutAway] = useStoredSwitch(LIST_HIDDEN_KEY);
  const [over, setOver] = useState(false);
  const listShown = wide ? !putAway : over;
  const listButton = useRef<HTMLButtonElement>(null);

  // The mark on the page's root element says what the switch says, from the page's
  // start on: the script at the start of the body set it before the first paint
  // (lib/frame.ts), and the list's place, decided by it, is shown again after a
  // log-out, with no page loaded in between.
  //
  // Before the browser paints, here and below: when React can't take the page over as
  // the server sent it, it draws the page anew and takes every attribute off the
  // root, both marks with them. Put back only after the paint, a frame showed the
  // list's place to someone who had put it away, and the system's scheme to someone
  // who had chosen the other (ADR 0015, revision "the reader chooses light or dark").
  useLayoutEffect(() => {
    markListAway(putAway);
  }, [putAway]);

  // Light or dark, as chosen in another window of this browser, or while the browser
  // kept this page away: followed for as long as the app is on the page, whatever is
  // on it, also with the list and its choice put away (lib/scheme.ts). The mark of the
  // scheme isn't kept like the list's above: it changes only where the choice does,
  // and once here, where it has come apart from what the browser remembers.
  useLayoutEffect(() => followChoicesMadeElsewhere(), []);

  // Where the reader left each conversation is kept for whoever has the session, in the
  // tab too: when the session changes hands, the last one's places go at once, and not
  // when a conversation next asks for one (lib/chat/places.ts).
  const { holder } = session;
  useEffect(() => {
    holdPlacesFor(holder);
  }, [holder]);

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
              // It lies over all of the chat: the menu of models (z-10) included,
              // which may be open while the keyboard is in it.
              <button
                type="button"
                aria-label="Close the conversations"
                tabIndex={-1}
                onClick={() => {
                  setOver(false);
                }}
                className="absolute inset-0 z-20 cursor-default bg-black/40"
              />
            )}
            {listShown && (
              <aside
                className={`flex flex-col ${LIST_PLACE} ${
                  wide
                    ? ""
                    : "absolute inset-y-0 left-0 z-30 max-w-[85%] shadow-xl"
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
  // The reader's choice of the model that answers: this browser's, one for every
  // conversation, and so held here, above each (ADR 0032).
  const [chosen, choose] = useChosen();
  const { list: models, readAgain } = useModels(holder, usage.standIn, chosen);
  const { model: answers, named } = answering(models, usage.standIn, chosen);
  // The model that answers is loaded at its server ahead of the first question, once
  // the page knows it and it is ready: also when it becomes ready, and when another
  // model answers. Nothing on the page waits for it, or says so (ADR 0026).
  useEffect(() => {
    warmUp(holder, answers);
  }, [holder, answers]);
  // Made here, whole, and placed by the conversation: what the picker does on its own
  // never draws the conversation again, and the conversation's renders, one for each
  // piece of an answer, leave the picker alone (`ModelPicker`). With one model on offer
  // there is nothing to choose, and no picker.
  const picker =
    models !== null && models.models.length > 1 && answers !== null ? (
      <ModelPicker
        models={models.models}
        answering={answers}
        onChoose={(model) => {
          choose(model);
          // The reader's newest decision counts: a model that stood in at a usage
          // limit answers no longer, and the limit stays.
          if (usage.standIn !== null) {
            usage.endStandIn();
          }
        }}
        onOpen={readAgain}
      />
    ) : null;
  const chat = {
    id,
    holder,
    model: answers,
    named,
    picker,
    models: models?.models ?? null,
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
  holder,
  model,
  named,
  picker,
  models,
  offers,
  usage,
  onExists,
  onActivity,
  onStartNew,
  onStartOver,
}: {
  id: string;
  holder: string | null;
  model: Model | null;
  named: string | null;
  picker: ReactElement | null;
  models: readonly Model[] | null;
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
  // the page would jump. But not for long: once the page's wait for that code is over,
  // which began when the conversation was read unless something else on the page began
  // it before, they are shown as written.
  const held = useWaitsForRenderer(loaded.state === "ready");

  if (loaded.state === "loading" || held) {
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
      holder={holder}
      model={model}
      named={named}
      picker={picker}
      models={models}
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

/** The models on offer, with whether each can answer right now (`list`), nothing until
 * the API has said, and a way to read them again, which nothing waits for. Asked again
 * and again while the one that answers can't, whichever answers: the one that stands
 * in at a usage limit (`standIn`), the reader's choice (`chosen`), or the default.
 *
 * Held under the session it was read in, like everything read from the API: a new
 * session reads it anew, instead of looking where the last one's answer was. */
function useModels(
  holder: string | null,
  standIn: string | null,
  chosen: string | null,
): { list: ModelList | null; readAgain: () => void } {
  const { data, mutate } = useSWR(
    holder === null ? null : (["models", holder] as const),
    listModels,
    {
      refreshInterval: (latest: ModelList | undefined) =>
        latest !== undefined &&
        answering(latest, standIn, chosen).model?.status !== "ready"
          ? UNREADY_POLL_MS
          : 0,
    },
  );
  const readAgain = useCallback(() => {
    void mutate();
  }, [mutate]);
  return { list: data ?? null, readAgain };
}
