// The reader's choice of the model that answers (lib/chat/chosen.ts): what this browser
// keeps of it, and which model answers and what a question names, from it, from the
// model that stands in at a usage limit, and from the models on offer.
import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ModelList } from "@/lib/api/client";
import { freeModel, model, secondModel } from "@/tests/fake-api";

import {
  answering,
  CHOSEN_KEY,
  chosenIn,
  MODEL_ID_MAX,
  useChosen,
} from "./chosen";

/** The installation's three models: the default, one that costs nothing, and a third. */
const OFFERED: ModelList = {
  default: "standard",
  models: [model(), freeModel(), secondModel()],
};

describe("chosenIn", () => {
  it("believes what can be a model's id", () => {
    expect(chosenIn("local")).toBe("local");
    expect(chosenIn("x")).toBe("x");
    expect(chosenIn("m".repeat(MODEL_ID_MAX))).toBe("m".repeat(MODEL_ID_MAX));
  });

  it("believes nothing else", () => {
    expect(chosenIn(null)).toBeNull();
    expect(chosenIn("")).toBeNull();
    expect(chosenIn("m".repeat(MODEL_ID_MAX + 1))).toBeNull();
  });
});

describe("useChosen", () => {
  it("is nobody's choice until one is made, and keeps none", () => {
    const { result } = renderHook(() => useChosen());

    expect(result.current[0]).toBeNull();
    expect(window.localStorage.getItem(CHOSEN_KEY)).toBeNull();
  });

  it("keeps a choice in this browser, where the next page reads it", () => {
    const { result } = renderHook(() => useChosen());

    act(() => {
      result.current[1]("local");
    });

    expect(result.current[0]).toBe("local");
    expect(window.localStorage.getItem(CHOSEN_KEY)).toBe("local");
    expect(renderHook(() => useChosen()).result.current[0]).toBe("local");
  });

  it("reads nothing kept that can't be a model's id", () => {
    window.localStorage.setItem(CHOSEN_KEY, "m".repeat(MODEL_ID_MAX + 1));

    expect(renderHook(() => useChosen()).result.current[0]).toBeNull();
  });

  it("holds a choice it can't keep, when the browser's storage is off limits", () => {
    for (const method of ["getItem", "setItem"] as const) {
      vi.spyOn(Storage.prototype, method).mockImplementation(() => {
        throw new DOMException("The operation is insecure.", "SecurityError");
      });
    }
    const { result } = renderHook(() => useChosen());
    expect(result.current[0]).toBeNull();

    act(() => {
      result.current[1]("local");
    });

    expect(result.current[0]).toBe("local");
  });
});

describe("answering", () => {
  it("is the default, named by none, while nobody chose", () => {
    expect(answering(OFFERED, null, null)).toEqual({
      model: model(),
      named: null,
    });
  });

  it("is the reader's choice, named by its id", () => {
    expect(answering(OFFERED, null, "second")).toEqual({
      model: secondModel(),
      named: "second",
    });
    // The default chosen is a choice too.
    expect(answering(OFFERED, null, "standard")).toEqual({
      model: model(),
      named: "standard",
    });
  });

  it("is the one that stands in at a usage limit, before the reader's choice", () => {
    expect(answering(OFFERED, "local", "second")).toEqual({
      model: freeModel(),
      named: "local",
    });
    expect(answering(OFFERED, "local", null)).toEqual({
      model: freeModel(),
      named: "local",
    });
  });

  it("passes over what isn't on offer", () => {
    expect(answering(OFFERED, null, "gone")).toEqual({
      model: model(),
      named: null,
    });
    expect(answering(OFFERED, "gone", "second")).toEqual({
      model: secondModel(),
      named: "second",
    });
    expect(answering(OFFERED, "gone", "gone")).toEqual({
      model: model(),
      named: null,
    });
  });

  it("names the reader's choice before the models are read, and knows no model", () => {
    expect(answering(null, null, "second")).toEqual({
      model: null,
      named: "second",
    });
    expect(answering(null, null, null)).toEqual({ model: null, named: null });
  });
});
