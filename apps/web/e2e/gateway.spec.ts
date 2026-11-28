// The developer gateway through the whole stack (ADR 0034): a key made in the account's
// place on the page lists the models with OpenAI's own JavaScript SDK, pointed at the
// stack's address with /v1 and trying nothing twice, and the gateway's refusals are
// OpenAI's errors, which the SDK reads, the proxy's refusal of a body too large among
// them. What each route answers, member by member, is the API's tests'
// (apps/api/tests/test_gateway_api.py).
import { randomInt } from "node:crypto";

import OpenAI, { AuthenticationError, NotFoundError } from "openai";

import { anAccountsKeys, make } from "./api-keys";
import { expect, test } from "./chat";

/** OpenAI's SDK with `key`, at the gateway of the stack at `baseURL`. */
function gateway(key: string, baseURL: string | undefined): OpenAI {
  return new OpenAI({
    apiKey: key,
    baseURL: `${baseURL ?? ""}/v1`,
    maxRetries: 0,
  });
}

/** A text of a key's form that no key has: drawn now, as a key is. */
function neverMade(): string {
  const drawn =
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";
  const run = (length: number) =>
    Array.from({ length }, () => drawn.charAt(randomInt(drawn.length))).join(
      "",
    );
  return `act_${run(8)}_${run(43)}`;
}

/** What `asked` was refused with: the test fails if it was answered. */
async function refusal(asked: Promise<unknown>): Promise<unknown> {
  try {
    await asked;
  } catch (error) {
    return error;
  }
  throw new Error("It was answered, where it was to be refused.");
}

test("a key made in the page lists the models with OpenAI's SDK, as the page's own list names them", async ({
  chat,
  page,
  baseURL,
}) => {
  const { keys } = await anAccountsKeys(chat, page);
  const sdk = gateway(await make(keys, "SDK"), baseURL);

  const listed: string[] = [];
  for await (const model of sdk.models.list()) {
    listed.push(model.id);
  }
  const one = await sdk.models.retrieve("standard");
  // What the page reads, with its session.
  const forThePage = await page.request.get("/api/models");
  expect(forThePage.status()).toBe(200);
  const { models } = (await forThePage.json()) as { models: { id: string }[] };

  expect(listed).toEqual(models.map((model) => model.id));
  expect(one).toEqual({
    id: "standard",
    object: "model",
    created: 0,
    owned_by: "actio",
  });

  // Also an id with a slash in it, which the SDK sends as one id, the slash encoded, and
  // the proxy passes on as it came.
  for (const asked of ["zeppelin", "openai/gpt-oss-20b"]) {
    const nobodyOffers = await refusal(sdk.models.retrieve(asked));
    expect(nobodyOffers, asked).toBeInstanceOf(NotFoundError);
    expect(nobodyOffers, asked).toMatchObject({
      status: 404,
      type: "invalid_request_error",
      code: "model_not_found",
      param: null,
      message: "404 No model is called that. GET /v1/models lists them.",
    });
  }
});

test("a key no row has is the SDK's authentication error, and a body too large for the proxy is OpenAI's error", async ({
  baseURL,
  request,
}) => {
  const unknown = await refusal(gateway(neverMade(), baseURL).models.list());
  expect(unknown).toBeInstanceOf(AuthenticationError);
  expect(unknown).toMatchObject({
    status: 401,
    type: "invalid_request_error",
    code: "invalid_api_key",
    param: null,
    message: "401 This API key isn't known: it may be mistyped, or revoked.",
  });

  // Two and a half million bytes: more than the proxy lets through in one request, which
  // it refuses before any of it reaches the API.
  const tooLarge = await request.post("/v1/models", {
    data: "a".repeat(2_500_000),
    headers: { "Content-Type": "application/json" },
  });
  expect(tooLarge.status()).toBe(413);
  expect(tooLarge.headers()["content-type"]).toBe("application/json");
  expect(await tooLarge.json()).toEqual({
    error: {
      message:
        "This is too much to send at once: Actio takes at most 2 MB in one request.",
      type: "invalid_request_error",
      param: null,
      code: null,
    },
  });
});
