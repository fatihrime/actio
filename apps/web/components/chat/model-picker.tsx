"use client";

import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";

import { CheckIcon, ChevronUpIcon } from "@/components/icons";
import type { Model } from "@/lib/api/client";
import { notReadyInShort } from "@/lib/chat/outcomes";

/** The room kept between the menu and the button under it, and between the menu and
 * the window's top and right edges. */
const GAP_PX = 8;

/** The keys that move the keyboard in the picker, or choose. Once one of them is used,
 * the row the keyboard is on is outlined (`data-keys`). */
const KEYS = new Set(["ArrowDown", "ArrowUp", "Home", "End", "Enter", " "]);

/** Which of the models on offer answers: a button in the row under the box, which says
 * the title of the model that answers, and opens a menu of the models above it (ADR
 * 0032). The reader's choice, and what follows from it, is `ChatApp`'s; this is how it
 * is made.
 *
 * Written here, after the ARIA Authoring Practices' menu button. On the button, Enter,
 * Space and a click open and close the menu, and ArrowDown and ArrowUp open it. Opening
 * puts the keyboard on the checked row. In the menu, ArrowDown and ArrowUp move, round
 * the ends, and Home and End go to them; Enter, Space and a click choose and close, and
 * Escape closes. Each of those three puts the keyboard back on the button. Tab closes,
 * and the keyboard goes where Tab takes it. A press outside closes, and moves the
 * keyboard nowhere; a press on the menu's own edge, between its rows, leaves the
 * keyboard in the menu. No type-ahead. Every button is `type="button"`: the picker is
 * inside the form of the box, where any other would send the question.
 *
 * WebKit puts the keyboard on no button that is clicked, so nothing here rests on the
 * button having it after a click: the keyboard is put on a row, and back on the button,
 * by asking; and a press outside closes by the pointer, never by the keyboard leaving.
 *
 * Whether the menu is open, and where the keyboard is in it, are this component's own
 * state. It stands in the conversation's row under the box, and `Chat`, which maps
 * every message, is not drawn again when they change: it is given the picker whole,
 * made by `ChatApp`, and draws it where it goes. No test can hold that cheaply: a
 * review reads for it. */
export function ModelPicker({
  models,
  answering,
  onChoose,
  onOpen,
}: {
  /** The models on offer, in the order of the list. */
  models: readonly Model[];
  /** The model that answers: the checked row. */
  answering: Model;
  /** The reader chose `model`, by its id. */
  onChoose: (model: string) => void;
  /** The menu opens: what is known of the models is read again, and nothing waits for
   * it. */
  onOpen: () => void;
}) {
  const [open, setOpen] = useState(false);
  // How high and how wide the menu may be: the room above the button, and from the
  // button's left edge to the window's right one, measured as it opens. A title may be
  // 80 characters long: wider than that room, the menu made the whole page scroll
  // sideways, and its rows fold their titles instead.
  const [room, setRoom] = useState({ height: 0, width: 0 });
  // Whether a key of the picker's own was the last thing used in it, and not the
  // pointer. Where the browser says that the keyboard is in use, the outline is drawn
  // anyway (`:focus-visible`); not every engine says so of a button that a script
  // puts the keyboard on, as `ThemeChoice` found of WebKit's radio buttons.
  const [byKeys, setByKeys] = useState(false);
  const here = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const menuId = useId();

  function show() {
    const at = button.current?.getBoundingClientRect();
    setRoom({
      height: Math.max(0, (at?.top ?? 0) - 2 * GAP_PX),
      width: Math.max(
        0,
        document.documentElement.clientWidth - (at?.left ?? 0) - GAP_PX,
      ),
    });
    setOpen(true);
    onOpen();
  }

  /** Close the menu, with the keyboard back on the button. */
  function close() {
    button.current?.focus();
    setOpen(false);
  }

  // The keyboard goes to the checked row once the menu is shown, and not before: a
  // browser puts it on nothing inside an element that is hidden.
  useLayoutEffect(() => {
    if (!open) {
      return;
    }
    const rows = rowsOf(menu.current);
    (
      rows.find((row) => row.getAttribute("aria-checked") === "true") ?? rows[0]
    )?.focus();
  }, [open]);

  // A press anywhere outside the picker closes the menu, and leaves the keyboard where
  // the press puts it.
  useEffect(() => {
    if (!open) {
      return;
    }
    const pressed = (event: PointerEvent) => {
      const inside =
        event.target instanceof Node &&
        here.current?.contains(event.target) === true;
      if (!inside) {
        setOpen(false);
      }
    };
    document.addEventListener("pointerdown", pressed, true);
    return () => {
      document.removeEventListener("pointerdown", pressed, true);
    };
  }, [open]);

  function onMenuKey(event: KeyboardEvent<HTMLDivElement>) {
    const rows = rowsOf(menu.current);
    const at = rows.findIndex((row) => row === document.activeElement);
    const moveTo = (index: number) => {
      event.preventDefault();
      rows[(index + rows.length) % rows.length]?.focus();
    };
    switch (event.key) {
      case "ArrowDown":
        moveTo(at + 1);
        break;
      case "ArrowUp":
        moveTo(at < 0 ? -1 : at - 1);
        break;
      case "Home":
        moveTo(0);
        break;
      case "End":
        moveTo(-1);
        break;
      case "Tab":
        // The menu closes, and the browser moves the keyboard on from where the row
        // was: to what follows the picker, or with Shift back to its button. Seen in
        // Chromium, Firefox and WebKit, which each start from a row hidden while
        // its key is handled as from one that is shown.
        setOpen(false);
        break;
    }
  }

  return (
    <div
      ref={here}
      className="relative min-w-0"
      onKeyDown={(event) => {
        if (KEYS.has(event.key)) {
          setByKeys(true);
        }
        if (event.key === "Escape" && open) {
          // It closes the menu, and nothing else: the list of conversations that lies
          // over a narrow window's chat, which Escape puts away (`ChatApp`), stays.
          event.stopPropagation();
          close();
        }
      }}
      onPointerDown={() => {
        setByKeys(false);
      }}
    >
      <button
        ref={button}
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={menuId}
        aria-label={`Model: ${answering.title}`}
        onClick={() => {
          if (open) {
            close();
          } else {
            show();
          }
        }}
        onKeyDown={(event) => {
          if (!open && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
            event.preventDefault();
            show();
          }
        }}
        // As high as Send beside it, so that the row is no higher with it.
        className="flex max-w-full min-w-0 items-center gap-1 rounded-lg border border-transparent px-2 py-1.5 text-sm text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring aria-expanded:bg-muted aria-expanded:text-foreground"
      >
        <span className="truncate">{answering.title}</span>
        <ChevronUpIcon />
      </button>
      {/* On the page from when the button is, and hidden while it is closed. Above the
          button, since the box is at the page's foot, and above whatever of the
          conversation it lies over: the buttons on a block of code are on the layer it
          is on, z-10, and come before it on the page. Never higher than the room above
          the button: it scrolls inside itself when the list is longer. Never wider
          than the room to the window's right edge. It can take the keyboard itself,
          which a press on its own edge, between its rows, gives it: on nothing that
          can, the press took the keyboard out of the picker, whose keys then did
          nothing, Escape among them, while the menu stayed open. */}
      <div
        ref={menu}
        id={menuId}
        role="menu"
        aria-label="Model"
        hidden={!open}
        tabIndex={-1}
        data-keys={byKeys ? "" : undefined}
        onKeyDown={onMenuKey}
        style={{ maxHeight: room.height, maxWidth: room.width }}
        className="absolute bottom-full left-0 z-10 mb-2 w-max min-w-48 overflow-y-auto rounded-lg border border-border bg-card p-1 shadow-lg outline-none"
      >
        {models.map((model) => (
          <Choice
            key={model.id}
            model={model}
            checked={model.id === answering.id}
            onChoose={() => {
              close();
              onChoose(model.id);
            }}
          />
        ))}
      </div>
    </div>
  );
}

/** One model of the menu: its title, and under it, when it can't answer right now, why,
 * in a few words. It can be chosen all the same: chosen, the page says what to do about
 * it. The one that answers has a tick, in the colour of the words, which shows where a
 * system's own few colours take the place of the page's too. */
function Choice({
  model,
  checked,
  onChoose,
}: {
  model: Model;
  checked: boolean;
  onChoose: () => void;
}) {
  const id = useId();
  const why = notReadyInShort(model);
  return (
    <button
      type="button"
      role="menuitemradio"
      aria-checked={checked}
      tabIndex={-1}
      aria-labelledby={`${id}title`}
      aria-describedby={why === null ? undefined : `${id}why`}
      onClick={onChoose}
      className="flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-muted focus:bg-muted focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring in-data-keys:focus:outline-2 in-data-keys:focus:-outline-offset-2 in-data-keys:focus:outline-ring"
    >
      <span className="mt-0.5 flex size-4 shrink-0">
        {checked && <CheckIcon />}
      </span>
      <span className="min-w-0">
        <span id={`${id}title`} className="block">
          {model.title}
        </span>
        {why !== null && (
          <span id={`${id}why`} className="block text-xs text-muted-foreground">
            {why}
          </span>
        )}
      </span>
    </button>
  );
}

/** The menu's rows, in their order. */
function rowsOf(menu: HTMLElement | null): HTMLElement[] {
  return menu === null
    ? []
    : Array.from(menu.querySelectorAll<HTMLElement>('[role="menuitemradio"]'));
}
