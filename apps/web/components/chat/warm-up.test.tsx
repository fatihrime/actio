// The model that answers, loaded at its server ahead of the first question to it
// (lib/chat/warm-up.ts, ADR 0026), driven as a person would: through the whole page,
// against the fake API (tests/fake-api.ts).
//
// The page asks at most once a minute for one session and model, and keeps when it
// last asked for as long as it is open. So each test loads the app's modules anew, as
// a page that has just been opened does.
import { screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { clockAt, MINUTE, SECOND } from "@/tests/clock";
import {
  account,
  fakeApi,
  freeModel,
  json,
  model,
  models,
  problem,
  refused,
  streamed,
} from "@/tests/fake-api";
import type { Call } from "@/tests/fake-api";

const WARM_UP = "POST /api/models/*/warm-up";
const STANDARD = "/api/models/standard/warm-up";
const MODELS = "GET /api/models";
const TURNS = "POST /api/conversations/*/turns";
const CONVERSATION = "GET /api/conversations/*";
const LOG_IN = "POST /api/session";

/** How long the page takes, at the most, to read the models again while the one that
 * answers can't: it looks every five seconds. */
const NEXT_LOOK = { timeout: 8 * SECOND };

/** What the page wrote to the console, as errors and as warnings. */
let toConsole: {
  error: ReturnType<typeof vi.spyOn>;
  warn: ReturnType<typeof vi.spyOn>;
};

beforeEach(() => {
  // Every module is loaded anew by the next import, as on a page that was just opened.
  vi.resetModules();
  toConsole = {
    error: vi.spyOn(console, "error"),
    warn: vi.spyOn(console, "warn"),
  };
});

afterEach(() => {
  // The page says nothing of how a load went, also not to the console.
  expect(toConsole.error).not.toHaveBeenCalled();
  expect(toConsole.warn).not.toHaveBeenCalled();
});

/** The app, as a page that has just been opened. */
async function newPage() {
  return import("@/tests/open");
}

/** Where the page's requests to load a model went, in order. */
function loaded(calls: Call[]): string[] {
  return calls.map((call) => `${call.method} ${call.path}`);
}

describe.each([
  { how: "", strict: false },
  // React runs each effect, undoes it and runs it again when the page appears.
  { how: " under React's StrictMode", strict: true },
])("the model that answers$how", ({ strict }) => {
  it("is asked to be loaded once, as soon as the page knows it", async () => {
    const api = fakeApi();
    const { open } = await newPage();

    open("/", { strict });

    await waitFor(() => {
      expect(api.to(WARM_UP)).toHaveLength(1);
    });
    // What comes with the session is on the page by now, and was asked for along
    // with it: nothing has asked again.
    await screen.findByRole("region", { name: "Usage" });
    expect(await screen.findByLabelText("Message")).toBeEnabled();
    expect(loaded(api.to(WARM_UP))).toEqual([`POST ${STANDARD}`]);
    expect(api.to(WARM_UP)[0]?.body).toBeUndefined();
  });
});

describe("a model that is asked to be loaded", () => {
  it("isn't, while it can't answer, and is once it can", async () => {
    const api = fakeApi().on(
      MODELS,
      () => json(200, models({ status: "missing", thinking: false })),
      () => json(200, models()),
    );
    const { open } = await newPage();
    const user = open();
    await screen.findByText("Qwen 3.5 4B isn't downloaded yet.");

    // Something written while it can't answer asks nothing either.
    await user.type(screen.getByLabelText("Message"), "Hello");
    const whileMissing = api.to(WARM_UP).length;

    await waitFor(() => {
      expect(api.to(WARM_UP)).toHaveLength(1);
    }, NEXT_LOOK);
    expect(whileMissing).toBe(0);
    expect(api.to(MODELS)).toHaveLength(2);
    expect(
      screen.queryByText("Qwen 3.5 4B isn't downloaded yet."),
    ).not.toBeInTheDocument();
  }, 15_000);

  it("isn't asked again when something is written within the minute, and is after it", async () => {
    const clock = clockAt();
    const api = fakeApi().on(TURNS, () => streamed("answer.sse"));
    const { open } = await newPage();
    const user = open();
    const box = await screen.findByLabelText("Message");
    await waitFor(() => {
      expect(api.to(WARM_UP)).toHaveLength(1);
    });

    clock.advance(59 * SECOND);
    await user.type(box, "Why is");
    const withinTheMinute = api.to(WARM_UP).length;
    clock.advance(SECOND);
    await user.type(box, " the sky blue?");

    await waitFor(() => {
      expect(api.to(WARM_UP)).toHaveLength(2);
    });
    expect(withinTheMinute).toBe(1);
    // One request at the most for all that was written after the minute was over.
    await user.type(box, "{Enter}");
    await screen.findByText("Echo: Why is the sky blue?");
    expect(loaded(api.to(WARM_UP))).toEqual([
      `POST ${STANDARD}`,
      `POST ${STANDARD}`,
    ]);
  });

  it("is the one that stands in at a usage limit, once it answers", async () => {
    clockAt();
    const api = fakeApi()
      .on(MODELS, () =>
        json(200, { default: "standard", models: [model(), freeModel()] }),
      )
      .on(TURNS, () => refused("limit_reached.json"))
      .on(CONVERSATION, () => problem(404, "No such conversation."));
    const { ask, open } = await newPage();
    const user = open();
    await ask(user, "Why is the sky blue?");

    await user.click(
      await screen.findByRole("button", { name: "Use Local (free)" }),
    );

    await waitFor(() => {
      expect(loaded(api.to(WARM_UP))).toEqual([
        `POST ${STANDARD}`,
        "POST /api/models/local/warm-up",
      ]);
    });
  });

  it("is asked for anew in a new session, also within the minute", async () => {
    clockAt();
    const api = fakeApi().on(LOG_IN, () => json(200, account()));
    const { open } = await newPage();
    const user = open();
    await waitFor(() => {
      expect(api.to(WARM_UP)).toHaveLength(1);
    });

    await user.click(await screen.findByRole("link", { name: "Log in" }));
    await user.type(
      await screen.findByLabelText("Email address"),
      "ada@example.com",
    );
    await user.type(
      screen.getByLabelText("Password"),
      "correct horse battery{Enter}",
    );

    await screen.findByRole("link", { name: "Account: ada@example.com" });
    await waitFor(() => {
      expect(loaded(api.to(WARM_UP))).toEqual([
        `POST ${STANDARD}`,
        `POST ${STANDARD}`,
      ]);
    });
  });
});

describe("what comes of a request to load a model", () => {
  it.each([
    {
      // What a server that can load ahead is answered: accepted, with no body.
      what: "a load that was started",
      answer: () => new Response(null, { status: 202 }),
    },
    {
      what: "a refusal at the rate limit",
      answer: () =>
        problem(
          429,
          "Too many requests to load a model. Try again in 30 minutes.",
        ),
    },
    {
      what: "a model that can't answer",
      answer: () =>
        problem(503, "This model's server can't be reached.", {
          model: "standard",
          model_status: "unreachable",
        }),
    },
    {
      what: "a model nobody offers any more",
      answer: () =>
        problem(404, "No model is called that. GET /api/models lists them."),
    },
    {
      what: "a request that fails",
      answer: (): Response => {
        throw new TypeError("fetch failed");
      },
    },
  ])("changes nothing on the page: $what", async ({ answer }) => {
    const api = fakeApi()
      .on(WARM_UP, answer)
      .on(TURNS, () => streamed("answer.sse"));
    const { ask, open } = await newPage();
    const user = open();
    await waitFor(() => {
      expect(api.to(WARM_UP)).toHaveLength(1);
    });

    await ask(user, "Why is the sky blue?");

    expect(
      await screen.findByText("Echo: Why is the sky blue?"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByText(/load a model/)).not.toBeInTheDocument();
    expect(screen.queryByText(/can't be reached/)).not.toBeInTheDocument();
    expect(screen.queryByText(/GET \/api\/models/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument();
    expect(api.to(TURNS)).toHaveLength(1);
  });
});

it("waits a minute counted from the last time it asked, whatever came of it", async () => {
  const clock = clockAt();
  const api = fakeApi().on(WARM_UP, () =>
    problem(429, "Too many requests to load a model. Try again in 30 minutes."),
  );
  const { open } = await newPage();
  const user = open();
  const box = await screen.findByLabelText("Message");
  await waitFor(() => {
    expect(api.to(WARM_UP)).toHaveLength(1);
  });

  await user.type(box, "Hello");
  const refusedThen = api.to(WARM_UP).length;
  clock.advance(MINUTE);
  await user.type(box, "!");

  await waitFor(() => {
    expect(api.to(WARM_UP)).toHaveLength(2);
  });
  expect(refusedThen).toBe(1);
});

it("asks again at once when the clock was set back, and doesn't wait for it to catch up", async () => {
  const clock = clockAt();
  const api = fakeApi();
  const { open } = await newPage();
  const user = open();
  const box = await screen.findByLabelText("Message");
  await waitFor(() => {
    expect(api.to(WARM_UP)).toHaveLength(1);
  });

  clock.advance(-2 * MINUTE);
  await user.type(box, "Hello");

  await waitFor(() => {
    expect(api.to(WARM_UP)).toHaveLength(2);
  });
});
