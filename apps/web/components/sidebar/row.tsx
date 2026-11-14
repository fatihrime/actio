"use client";

import { useEffect, useRef, useState } from "react";
import type { MouseEvent } from "react";

import { BinIcon, PencilIcon } from "@/components/icons";
import type { ConversationSummary } from "@/lib/api/client";
import { conversationPath } from "@/lib/location";

/** The longest title the API takes (`Rename` in its description). A test holds this
 * to the API's own number. */
export const TITLE_MAX = 200;

/** What a conversation is listed as. A new one is named after its first question; one
 * without a title has had no question with words in it. */
export function labelOf(conversation: ConversationSummary): string {
  return conversation.title ?? "New conversation";
}

const SMALL_BUTTON =
  "rounded-md p-1.5 text-muted-foreground hover:bg-background hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring";

/** One conversation in the list: a link that opens it, and the buttons to rename and
 * to delete it.
 *
 * Renaming happens in place: the link becomes a box to type in. Deleting asks first,
 * in place too, because there is no way back.
 */
export function Row({
  conversation,
  current,
  onOpen,
  onRename,
  onDelete,
}: {
  conversation: ConversationSummary;
  /** Whether this is the conversation on the page. */
  current: boolean;
  onOpen: () => void;
  /** Both say whether it was done. What went wrong is shown by whoever does it. */
  onRename: (title: string) => Promise<boolean>;
  onDelete: () => Promise<boolean>;
}) {
  const [mode, setMode] = useState<
    "idle" | "renaming" | "saving" | "asking" | "deleting"
  >("idle");
  const label = labelOf(conversation);

  // After renaming, or deciding not to delete, the keyboard goes back to the button
  // it came from.
  const renameButton = useRef<HTMLButtonElement>(null);
  const deleteButton = useRef<HTMLButtonElement>(null);
  const goBackTo = useRef<"rename" | "delete" | null>(null);
  useEffect(() => {
    if (mode === "idle" && goBackTo.current !== null) {
      const button =
        goBackTo.current === "rename" ? renameButton : deleteButton;
      button.current?.focus();
      goBackTo.current = null;
    }
  }, [mode]);

  function open(event: MouseEvent) {
    // A click with a modifier key, or with another button, is the browser's: it opens
    // the conversation in a new tab or window, by the link's own address.
    if (
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    ) {
      return;
    }
    event.preventDefault();
    onOpen();
  }

  async function save(typed: string) {
    const title = typed.trim();
    if (title === "" || title === conversation.title) {
      goBackTo.current = "rename";
      setMode("idle"); // nothing to change
      return;
    }
    setMode("saving");
    if (await onRename(title)) {
      goBackTo.current = "rename";
      setMode("idle");
    } else {
      setMode("renaming"); // what was typed is still in the box
    }
  }

  async function remove() {
    setMode("deleting");
    if (!(await onDelete())) {
      goBackTo.current = "delete";
      setMode("idle");
    }
    // Deleted, this row is no longer on the page.
  }

  if (mode === "renaming" || mode === "saving") {
    return (
      <li>
        <RenameForm
          title={conversation.title ?? ""}
          saving={mode === "saving"}
          onSave={(typed) => void save(typed)}
          onCancel={(byKey) => {
            if (byKey) {
              goBackTo.current = "rename";
            }
            // Not while it is being saved: then the box is only waiting.
            setMode((now) => (now === "renaming" ? "idle" : now));
          }}
        />
      </li>
    );
  }
  if (mode === "asking" || mode === "deleting") {
    return (
      <li>
        <ConfirmDelete
          label={label}
          deleting={mode === "deleting"}
          onDelete={() => void remove()}
          onCancel={() => {
            goBackTo.current = "delete";
            setMode("idle");
          }}
        />
      </li>
    );
  }
  return (
    <li className="group relative">
      <a
        href={conversationPath(conversation.id)}
        onClick={open}
        aria-current={current ? "page" : undefined}
        title={label}
        // Room for the two buttons, when they show.
        className="peer block truncate rounded-lg px-3 py-2 text-sm group-focus-within:pr-16 group-hover:bg-muted group-hover:pr-16 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring aria-[current=page]:bg-muted aria-[current=page]:pr-16 aria-[current=page]:font-medium"
      >
        {label}
      </a>
      {/* Shown for the row the pointer or the keyboard is on, and for the conversation
          on the page: a finger has no pointer to hover with. Always there for the
          keyboard and for screen readers. */}
      <div className="absolute inset-y-0 right-1 flex items-center opacity-0 group-focus-within:opacity-100 group-hover:opacity-100 peer-aria-[current=page]:opacity-100">
        <button
          ref={renameButton}
          type="button"
          onClick={() => {
            setMode("renaming");
          }}
          aria-label={`Rename “${label}”`}
          title="Rename"
          className={SMALL_BUTTON}
        >
          <PencilIcon />
        </button>
        <button
          ref={deleteButton}
          type="button"
          onClick={() => {
            setMode("asking");
          }}
          aria-label={`Delete “${label}”`}
          title="Delete"
          className={SMALL_BUTTON}
        >
          <BinIcon />
        </button>
      </div>
    </li>
  );
}

/** The box a title is typed into. Enter saves; Escape, or leaving the box, doesn't. */
function RenameForm({
  title,
  saving,
  onSave,
  onCancel,
}: {
  title: string;
  saving: boolean;
  onSave: (typed: string) => void;
  /** `byKey` says that Escape was pressed, and not that the box was left. */
  onCancel: (byKey: boolean) => void;
}) {
  const box = useRef<HTMLInputElement>(null);
  useEffect(() => {
    box.current?.focus();
    box.current?.select();
  }, []);

  return (
    <form
      className="p-1"
      onSubmit={(event) => {
        event.preventDefault();
        if (!saving) {
          onSave(box.current?.value ?? "");
        }
      }}
    >
      <input
        ref={box}
        aria-label="Title"
        defaultValue={title}
        placeholder="A title for this conversation"
        maxLength={TITLE_MAX}
        // Not disabled while saving: a disabled box would drop the keyboard's focus.
        readOnly={saving}
        aria-busy={saving}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.stopPropagation(); // the list itself stays as it is
            onCancel(true);
          }
        }}
        onBlur={() => {
          onCancel(false);
        }}
        className="w-full rounded-md border border-input bg-card px-2 py-1 text-sm outline-none read-only:opacity-60 focus:border-ring"
      />
    </form>
  );
}

/** Asks before a conversation is deleted. */
function ConfirmDelete({
  label,
  deleting,
  onDelete,
  onCancel,
}: {
  label: string;
  deleting: boolean;
  onDelete: () => void;
  onCancel: () => void;
}) {
  // The keyboard lands on the harmless answer.
  const cancel = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    cancel.current?.focus();
  }, []);

  return (
    <div
      role="group"
      aria-label={`Delete “${label}”?`}
      onKeyDown={(event) => {
        if (event.key === "Escape" && !deleting) {
          event.stopPropagation();
          onCancel();
        }
      }}
      className="my-1 space-y-2 rounded-lg border border-border bg-card p-3 text-sm"
    >
      <p className="truncate font-medium" title={label}>
        {label}
      </p>
      <p className="text-muted-foreground">
        Delete this conversation? It can’t be brought back.
      </p>
      <div className="flex justify-end gap-2">
        <button
          ref={cancel}
          type="button"
          onClick={onCancel}
          disabled={deleting}
          className="rounded-md border border-input px-3 py-1 font-medium hover:bg-muted focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring disabled:opacity-40"
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={onDelete}
          disabled={deleting}
          className="rounded-md bg-destructive px-3 py-1 font-medium text-destructive-foreground hover:bg-destructive/85 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring disabled:opacity-60"
        >
          {deleting ? "Deleting…" : "Delete"}
        </button>
      </div>
    </div>
  );
}
