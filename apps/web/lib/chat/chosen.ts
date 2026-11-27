// The model the reader chose to answer, and from it which model answers and what a
// question names (ADR 0032).
//
// The choice is this browser's, one for every conversation, new or stored: kept in its
// storage from the moment the reader first chooses, and read when the page is loaded.
// Another tab reads it when it is loaded, and isn't told of a choice made here
// afterwards. It isn't the account's: another browser starts at the default.
import { useCallback, useState } from "react";

import type { Model, ModelList } from "@/lib/api/client";

/** Where this browser keeps the reader's choice, a model's id. No entry until the
 * reader chooses. */
export const CHOSEN_KEY = "actio.model";

/** The longest id a question may name: the API's own (`TurnRequest.model`), which a
 * test holds this to. */
export const MODEL_ID_MAX = 32;

/** What was kept under `CHOSEN_KEY`, if it can be a model's id: a string of 1 to
 * `MODEL_ID_MAX` characters. Anything else is nobody's choice. */
export function chosenIn(kept: string | null): string | null {
  return kept !== null && kept.length >= 1 && kept.length <= MODEL_ID_MAX
    ? kept
    : null;
}

/** The reader's choice, as this browser remembers it, and a way to make another. It is
 * read once, when the component that asks first renders: that is never in the HTML
 * the server sends, which can't know it. */
export function useChosen(): [string | null, (model: string) => void] {
  const [chosen, setChosen] = useState(() => {
    try {
      return chosenIn(window.localStorage.getItem(CHOSEN_KEY));
    } catch {
      return null; // storage is off limits, as in some private windows
    }
  });
  const choose = useCallback((model: string) => {
    setChosen(model);
    try {
      window.localStorage.setItem(CHOSEN_KEY, model);
    } catch {
      // Not remembered, then.
    }
  }, []);
  return [chosen, choose];
}

/** The model that answers, and what a question names. */
export interface Answering {
  /** The model that answers, once the models on offer are read. */
  model: Model | null;
  /** The id a question names, or nothing: then the API's default answers. */
  named: string | null;
}

/** Which model answers, and what a question names, from the models on offer (`list`,
 * nothing until they are read), `standIn`, the model that costs nothing chosen to carry
 * on with at a usage limit, and `chosen`, the reader's choice, each by id.
 *
 * The model that answers is, in this order: the one that stands in, the reader's
 * choice, each while the installation offers it, and the default. A question names
 * either of the first two by its id, and the default by naming none, as before anyone
 * chose: which model that is, is the API's to say. A choice that isn't on offer is
 * left where it is kept, unused.
 *
 * Before the models are read, nothing is known to answer, and a question names the
 * reader's choice: one sent in that moment must not be answered by another model than
 * the one the picker shows a moment later. */
export function answering(
  list: ModelList | null,
  standIn: string | null,
  chosen: string | null,
): Answering {
  if (list === null) {
    return { model: null, named: chosen };
  }
  const offered = (id: string | null) =>
    id === null ? undefined : list.models.find((model) => model.id === id);
  const picked = offered(standIn) ?? offered(chosen);
  if (picked !== undefined) {
    return { model: picked, named: picked.id };
  }
  return { model: offered(list.default) ?? null, named: null };
}
