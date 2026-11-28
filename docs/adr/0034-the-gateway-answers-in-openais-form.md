# 0034. The developer gateway answers in OpenAI's form, to any API key that works

- **Status:** accepted
- **Date:** 2026-11-28

## Context

The proxy has sent `/v1/*` to the API from the start, for a gateway that a program which speaks OpenAI's API could use as it uses OpenAI's ([ADR 0004](0004-local-development-stack.md)). Nothing answered there. An account can make API keys now, and one route takes them, `GET /api/key`, which says what a key is ([ADR 0033](0033-api-keys-kept-as-their-hashes.md)). This decision is the gateway's door: the key it takes, the form of what it answers and of its errors, and its first two routes, the list of the models on offer and one model.

What the choice had to fit:

- **The programs at the door are OpenAI's SDKs, and programs written as they are.** Read in the source of the versions in this repository, OpenAI's Python SDK 3.46.0 and its JavaScript SDK 7.65.0:
    - each chooses the class of the exception it raises by the status alone: 400, 401, 403, 404, 409, 422, 429, and 500 and over;
    - each takes the body's `error` member for the error, and reads `message`, `type`, `param` and `code` from it. Its message is the status and `message`: "Error code: 404 - …" in Python, "404 …" in JavaScript;
    - each tries a 408, a 409, a 429 and any status from 500 up twice more, unless it is told otherwise.
- **OpenAI's published specification** (`openai/openai-openapi`, MIT) defines `Error` with four members, all of them required: `message` and `type`, strings, and `param` and `code`, each a string or null. `ErrorResponse` is `{"error": Error}`. A `Model` requires `id`, `object`, `created` and `owned_by`, and the list of them `object`, which is `list`, and `data`.
- **OpenAI's guide to its error codes** names four `type`s: `invalid_request_error`, for a 400; `rate_limit_error`, for a 429; `service_unavailable_error`, for a 503; and `insufficient_quota`. It names no `type` or `code` for a key it doesn't know, or a model that isn't there. Groq's layer, whose answers are recorded in this repository, answers the first with 401 and `invalid_api_key`, and the second with 404 and `model_not_found`, both of `type` `invalid_request_error`.
- **Every error the API answers is a problem document** (RFC 9457, [ADR 0009](0009-sessions-in-the-database.md)), raised as a `Problem` wherever it happens. The refusals of a key are raised by the dependency that recognizes it, `CurrentKey`, which `GET /api/key` uses. Given a problem document, each SDK raises the class for its status, with no `code` and `about:blank` as its `type`: the Python SDK's message is "Error code: 401", without the words, and the JavaScript SDK's the status and the whole document as JSON ("Measured").
- **A model has a name of Actio's own,** such as `standard`, by which everything asks for it, and a name on its provider's server, which can change behind it ([ADR 0013](0013-the-models-on-offer-are-a-file.md)).
- **What a model's server says of it is asked, and waited for,** by `GET /api/models`, which says whether each model can answer now, and what a question to it would meet ([ADR 0028](0028-a-model-names-those-that-stand-in-for-it.md), [ADR 0032](0032-the-reader-chooses-the-model-that-answers.md)).
- **One person maintains it:** no new dependency of the product, and no new setting.

## Options considered

**Where an error's form is decided:**

1. **Where it is raised:** each route under `/v1` raises OpenAI's error itself.
    - Con: the refusals of a key are raised by a dependency that `GET /api/key` shares, which would need a second form of each, or a second dependency.
    - Con: an error nobody thought of would leave as a problem document, which an SDK can't read: the framework's 404 for a path no route has, its 405 for a method a route doesn't take.
2. **Where the response is made, from the request's path.** Chosen.
    - Pro: code raises a `Problem` whatever the route, as before, and the handlers that make every error's response choose its form. One rule, which no route can leave out.
    - Con: what a `Problem` carries beyond its words, its extra members, has no place in OpenAI's form.

**What a model is called:**

1. **By its name in the models file,** such as `standard`. Chosen.
    - Pro: it is the name a request asks for it by, and stays the same when what runs behind it changes.
2. **By its name on its provider's server,** such as `fake-chat` or `openai/gpt-oss-20b`.
    - Con: two names of the file can be one model on one server, as `standard` and `local` are on the end-to-end stack, and a program could ask for neither by it.
    - Con: the name changes whenever the file gives the model another server.

**Whether the list asks the models' servers:**

1. **Yes,** and lists only the models that can answer now.
    - Con: a program that lists the models before it asks would wait for every server that wasn't asked lately, and find a model gone from the list while its server is away for a moment.
2. **No: the catalog alone.** Chosen.
    - Pro: answered at once, and the same each time, from what the installation offers. Whether a model can answer now is `GET /api/models`'s to say.

**What `created` says:**

1. **A moment of Actio's choosing,** such as when the process started, or the models file's date.
    - Con: it would read as when the model was made, which nothing here knows.
2. **0.** Chosen.
    - Pro: the member is there, as the specification and the SDKs require, and says nothing that isn't so. Its description says that it is always 0.

## Decision

**Under `/v1/`, any API key that works opens the gateway, and everything is answered in OpenAI's form: the list of the models on offer and one model, by their names in the models file, from the catalog alone, and every error, whoever raised it, as OpenAI's error. The form of an error is chosen where its response is made, from the request's path.**

**1. Any key that works opens the gateway.** A route under `/v1` takes `CurrentKey` as it is, and passes `NO_KEY` as its `responses`. No scope, and no flag in the plans file: the plan's `api_keys` says who has keys, and a key has nothing else to be kept from. A session's cookie is not looked at.

**2. Every error under `/v1/` is OpenAI's, whoever raised it.** `actio.api.problems` chooses the form where it makes the response, from the request's path (`on_the_gateway`), in the handlers of a `Problem` and of the framework's own errors. Code keeps raising `Problem`, and `get_key_use` is as it was.

- **The gateway is a path that starts with `/v1/`** (`problems.GATEWAY`): `/v1/models` is on it, and `/v10/models` isn't.
- **The error** is `{"error": {"message": …, "type": …, "param": …, "code": …}}`, always with the four members, as `application/json`, with the status and the headers the error had: `WWW-Authenticate` on a 401, `Allow` on a 405, and any other.
- **`message`** is the error's `detail`, word for word.
- **`type`** goes by the status, in one small table (`openai_format.error_type`): 429 `rate_limit_error`; 503 `service_unavailable_error`; any other status from 500 up `server_error`; every other status `invalid_request_error`. The first two and the last are the words of OpenAI's guide. `server_error` is this installation's own word for the rest, which the guide doesn't name.
- **`code`** is what the raiser gave, a keyword of `Problem` (`code=`), which a problem document never shows; and where it gave none, `invalid_api_key` for a 401, since under `/v1/` a 401 is always the key's. Otherwise `null`.
- **`param`** is `null`: no route under `/v1` takes a body.
- **A `Problem`'s extra members** have no place in OpenAI's error, and are left out under `/v1/`.
- **Left as it is, on purpose:** the handler of a request that isn't of the expected form, which answers a problem document on every path. No route under `/v1` can be refused so: none takes a body or a query, and a path's text is never refused.
- **Also as it is:** an exception nobody caught is answered 500 in plain text, on every route.

**3. The routes,** in `actio.api.gateway`, under the tag `gateway`:

| Route | Answers |
|---|---|
| `GET /v1/models` (`list_gateway_models`) | `{"object": "list", "data": […]}`: every model on offer, in the models file's order, as `GET /api/models` names them, the one that names conversations included |
| `GET /v1/models/{model}` (`get_gateway_model`) | one model, by all that follows in the path; 404 for a model nobody offers |

- **A model** is `{"id", "object": "model", "created", "owned_by"}`, exactly those four. `id` is its name in the models file, never its name on its provider's server; `created` is 0; `owned_by` is `actio`.
- **Every model is listed, whatever the key's plan:** the plans file has no way to keep a model from a plan.
- **No model's server is asked, and nothing is waited for:** the catalog alone, never `chains`, `chain_status`, `status` or `chain_as_known`.
- **A model nobody offers** is `Problem(404, "No model is called that. GET /v1/models lists them.", code="model_not_found")`. The id that was sent is not repeated: a path's text has no bound.
- **A model's id is all that follows `/v1/models/` in the path,** slashes included (`{model:path}`), looked up as it is. An SDK sends an id with a slash in it, such as `openai/gpt-oss-20b`, as one id, with the slash encoded, which the server decodes before it routes: a parameter that ended at a slash left such an id to the framework's own 404, "Not Found" with no `code` ("Measured"). No model of the file can be called so, a name there being lower-case letters, digits, `-` and `_`: it is a model nobody offers, as any other. A path that ends at `/v1/models/` names no model, and is answered the same: never with the default model, which the catalog gives for no name.
- **Nothing is counted against a rate limit:** a route that refuses whoever has no key that works is not one that anyone can call (ADR 0033).

**4. Where the code is.**

- `actio.api.openai_format`: OpenAI's form, written in one place, as `actio.api.ui_stream` is the one place for what a browser reads of a turn. The error and its table, the model and the list, as `Described` models for the API's description and as what the routes answer. It imports nothing from `actio.api.problems`.
- `actio.api.gateway`: the routes under `/v1`.
- `actio.api.problems`: its handlers answer in the form the request's surface has, and `Problem` takes `code`.

**5. The API's description** (`/api/docs`, `apps/api/openapi.json`, the web app's generated types):

- both routes are there, under `gateway`, with both ways to send a key as their security, as `GET /api/key` has;
- each error they answer is described with OpenAI's error as its body, `OpenAIErrorResponse` as `application/json`, never `Problem`, each with a description: 401 and 403 for both, 404 for the second. `describe` chooses by the path, as the handlers do;
- the 422 that the framework describes for a route with a path parameter is taken out on the gateway's paths: no route there answers one;
- the introduction says which routes take a key, and that under `/v1/` an error is OpenAI's.

**6. The proxy.** Its refusal of a body over 2 MB was a problem document on every route of the API. On the gateway it is OpenAI's error: `application/json`, `type` `invalid_request_error`, `param` and `code` `null`, with the same sentence but for its last: "Shorten the message, and send it again." is the chat's advice. In `deploy/caddy/Caddyfile`, inside `handle_errors 413`, a matcher on the request's path (`@gateway path /v1/*`) and a `handle` for it, before a `handle` that answers the problem document as before, byte for byte. Caddy 2.11.9 matches a request's path there. The matcher is the one by which the proxy sends `/v1/*` to the API, and its text is written from the API's own rule, which a test holds. It is not the API's rule to the letter: Caddy's also takes other letters' case and a doubled slash before it, `/V1/x` and `//v1/x`, where the API, which goes by the path's text as it routes it, has no route and answers a problem document ("Measured"). No SDK sends either.

**7. `make smoke`** checks the gateway too: `GET /v1/models` without a key, through the proxy, is answered 401 with `"code":"invalid_api_key"`.

**8. OpenAI's JavaScript SDK** is a dev dependency of the web app, `openai` from 7.65.0 on (`^7.65.0`, the lock file at 7.65.0), Apache-2.0 by its own `package.json`, with no dependencies of its own and Node 22 or newer, used by the end-to-end tests alone. Not pinned to the one version, as `ai` is, which the page ships: this one is a program at the door, and a newer one that reads an answer otherwise is to show as a failing test. OpenAI's Python SDK, `openai` 3.46.0, Apache-2.0, was a dev dependency of the workspace already, for fakellm's tests.

**Tests:**

- **the routes and the errors** (`apps/api/tests/test_gateway_api.py`), with a plain client and exact bodies: no key, a key no row has and two different keys, at each route, answered 401 as `application/json`, with the four members, `code` `invalid_api_key`, `type` `invalid_request_error`, `WWW-Authenticate: Bearer`, the words `GET /api/key` says for the same headers, and neither key in the answer; a key whose account's plan allows none, 403, with `code` `null`; a session's cookie and no key, 401; a key in `x-api-key` answered as one sent as a bearer; the list, from a catalog of four models, one of them the one that names conversations and one whose server isn't there, in the file's order, as `GET /api/models` names them; no model's server asked, by the list nor by a model that isn't ready; one model, and a model nobody offers, by a name of its own and by its name on its server, 404 with `model_not_found` and without the id sent; an id with a slash in it, encoded or not, and a path that ends at `/v1/models/`, the same 404; a path under `/v1/` that no route has, 404, and a method a route doesn't take, 405 with `Allow`, both OpenAI's; `/v10/models` and a path under `/api/` that no route has, problem documents as before; the table, through a small app that raises each of 400, 401, 403, 404, 405, 409, 413, 422, 429, 500, 502 and 503 under `/v1/`; and a `Problem` with extra members, a code and headers, whose headers go out on both surfaces, its code under `/v1/` alone and its extra members under `/api/` alone;
- **what OpenAI's Python SDK makes of them** (`apps/api/tests/test_gateway_sdk.py`), on the test's app in process, trying nothing twice: `models.list()` gives the models with the four members, in order, and `models.retrieve` one; a key no row has raises `AuthenticationError`, a key whose plan allows none `PermissionDeniedError`, and a model nobody offers `NotFoundError`, also by an id with a slash in it, each with its `type`, `code`, `param` and message;
- **the description** (`apps/api/tests/test_api_description.py`): every error on the gateway described with OpenAI's error, and every other as a problem document, as before; and no operation of the gateway claiming a 422;
- **the proxy** (`deploy/tests/test_compose_policy.py`): the refusal of a body too large in both forms, the gateway's matched by the API's own `GATEWAY`, with `type` by the API's own table, and the same sentence but for the chat's advice;
- **end to end** (`apps/web/e2e/gateway.spec.ts`), with OpenAI's JavaScript SDK pointed at the stack's own address with `/v1`, trying nothing twice: an account signs up and makes a key in its place on the page, and the spec reads the key the page shows; `models.list()` names the models `GET /api/models` names for the page's session, in its order, and `models.retrieve` reads one; a model nobody offers is the SDK's `NotFoundError`, with `model_not_found`, also by an id with a slash in it, which the proxy passes on as it came; a key no row has, drawn in the spec, its `AuthenticationError`, with `invalid_api_key`; and a body over 2 MB sent to `/v1/models` is answered by the proxy 413, as `application/json`, in OpenAI's form.

## Measured

On 2026-11-28, on the development machine, with OpenAI's Python SDK 3.46.0 and its JavaScript SDK 7.65.0.

- **What each SDK makes of the two forms,** given the same refusal, a 401 with the words of a key that isn't known, once as a problem document and once as OpenAI's error:
    - **a problem document:** both raise `AuthenticationError`, with no `code` and `about:blank` as its `type`. The Python SDK's message is "Error code: 401", and the words are in the error's `body` alone; the JavaScript SDK's is "401" and the whole document as JSON;
    - **OpenAI's error:** both raise `AuthenticationError` with `code` `invalid_api_key` and `type` `invalid_request_error`, and say the words: "Error code: 401 - This API key isn't known: it may be mistyped, or revoked." in Python, "401 This API key isn't known: it may be mistyped, or revoked." in JavaScript.
- **An id with a slash in it.** The Python SDK's `models.retrieve("openai/gpt-oss-20b")` asks for `/v1/models/openai%2Fgpt-oss-20b`, and the server routes the path decoded.
    - At a route whose parameter ends at a slash, as this one was first built, the framework answered it: 404, "Not Found", no `code`.
    - With the rest of the path as the id, it is `model_not_found`. A path that ends at `/v1/models/` then reaches the route with no id, for which the catalog's own lookup gives the default model: so the id is looked up as it is.
    - Through the proxy, the JavaScript SDK's same call is answered `model_not_found` too: the proxy passes the encoded slash on as it came.
- **The proxy's matcher,** in a container of the pinned image, Caddy 2.11.9, with a limit of 1 kB and a body of 3 kB, each path sent as written:
    - inside `handle_errors 413`, `@gateway path /v1/*` took `/v1/models` and `/v1/x/y`, and also `/V1/models`, `//v1/models`, `/v1//models`, `/v1/../v1/models` and `/%761/models`. `/api/keys` got the problem document, and `/v1` and `/v10/models` went to the web app, and weren't refused;
    - `path_regexp ^/v1/` in its place took the same, but for `/V1/models`: no closer to the API's rule, which takes neither `/V1/…` nor `//v1/…` for the gateway.
- **On the stack as `make up` runs it,** with the shipped config and its one model, as an account put on `dev`, with a key made for the look and revoked after it:
    - the list is 92 bytes, and goes out as it is: the proxy compresses from 512;
    - every answer was as decided, through the proxy: the list and the model by `Authorization` and by `x-api-key`; the three 401s with `WWW-Authenticate: Bearer`, a session's cookie alone among them, and none with the key that was sent; `model_not_found`; a path no route has, 404, with a key or without; a method a route doesn't take, 405 with `Allow: GET`; and a body of 3 MB, 413 in OpenAI's form at `/v1/models` and as the problem document at `/api/keys`. The same refusals under `/api/` were problem documents;
    - the Python SDK read the list as a page of `Model`s, and each refusal as its own error. With its retries as it ships them, it sent a key no row has once: a 401 isn't tried again;
    - `GET /v1/models` took 2.6 to 2.7 ms at the median and 3.5 to 4.1 ms at the 95th percentile, over 200 requests on one connection, twice; `GET /api/key` beside it, 2.5 to 2.6 ms and 4.3 to 4.6 ms. The list costs what recognizing its key costs;
    - the API's log said nothing of any of it but its access lines.
- **The end-to-end spec** passed 20 of 20: its two tests ten times over on one stack, the body of 2.5 MB answered 413 each time.
- **Mistakes were planted,** one at a time, in a scratch copy of the tree: 50 in the format, the routes, the handlers, the description and the Caddyfile, against the API's tests and the proxy's policy test, and 4 on a stack built from the copy, against the end-to-end spec. The tests of an id with a slash were written with the route's change, before any planting.
    - Of the 50, two failed nothing: a 401's own code said in the place of a code its raiser gave, which no route raises, and a path with `/v1/` further in taken for the gateway's. Each got a case, and fails it.
    - Seven mistakes in the description were caught first by the recorded description. With every test run, five also fail the test written for them. An empty description, which the framework fills with the status's own words, and a schema left out of the description fail the recorded description alone.
    - The 4 each fail the spec: the proxy's refusal as a problem document under `/v1/`; an id that ends at a slash; a 401 without its code; and a list in another order than the page's.

## Consequences

- **Easier:**
    - a program written for OpenAI's API, with an SDK of OpenAI's or one like them, lists the models of an installation with a key its owner made, and reads each refusal as one of its own kind, with its words and its code;
    - a route added under `/v1` answers its errors in OpenAI's form by raising a `Problem`, as any route does, and its description says so;
    - a program that lists the models is answered at once, whatever the models' servers are doing.
- **Harder:**
    - **two forms of error,** chosen by the path: a test of an error under `/v1/` reads OpenAI's, and one beside it a problem document;
    - **the proxy's refusal of a body too large is written twice,** once in each form, and a test holds the two together.
- **What it leaves:**
    - **nothing under `/v1` answers a question:** no route takes a conversation's messages;
    - **a `Problem`'s extra members are not said under `/v1/`,** such as a usage limit's `window` or a model's `model_status`: only its words, its status, its headers and its code;
    - **a request that isn't of the expected form has no form of its own there,** since none can be made: its refusal is a problem document on every path;
    - **an exception nobody caught is answered in plain text,** as on every route: an SDK raises its error for a 500, with no `code`;
    - **no answer names its request by an id:** the SDKs read `x-request-id`, which the API doesn't send, and find nothing;
    - **another site's page can't call `/v1` from a browser:** the API sends no CORS headers;
    - **every model is listed for every plan,** as every plan may ask every model.
- **How to reverse it:** take out `actio.api.gateway` and its router in `actio.app`, `actio.api.openai_format`, the gateway's branch of the handlers and of `describe` in `actio.api.problems`, with `GATEWAY`, `on_the_gateway` and `Problem`'s `code`; the `handle @gateway` of the Caddyfile's `handle_errors 413`; the smoke check; `apps/api/tests/test_gateway_api.py`, `apps/api/tests/test_gateway_sdk.py`, `apps/web/e2e/gateway.spec.ts` and `openai` among the web app's dev dependencies; and the changes to `test_api_description.py` and `test_compose_policy.py`. Then `make openapi`. Keys stay as they are.
