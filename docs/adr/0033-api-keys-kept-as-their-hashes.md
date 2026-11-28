# 0033. API keys, made by their account and kept as their hashes

- **Status:** accepted
- **Date:** 2026-11-28

## Context

A request says who is asking in one way: a session's cookie ([ADR 0009](0009-sessions-in-the-database.md)). That suits a browser, which keeps the cookie and sends it by itself. A program that works for an account, as the developer gateway that Actio is to have will let one ([ADR 0020](0020-usage-limits-on-turns.md), revision "an account has a plan of its own"), has no browser: it needs something its owner makes once, puts into the program's settings, and sends with each request. ADR 0009 named it among what was to point at the same accounts: "the developer API keys".

What the choice had to fit:

- **A key lives far longer than a session,** and goes where a cookie never does: into a script, a shell's history, a program's settings. It leaks more easily, and a copy of the database must not be one more way to have it.
- **A session's token is kept as its SHA-256,** found by it in one statement, and its "last seen" time is written at most once a minute (ADR 0009). The same reasons hold for anything random that says who is asking.
- **Which plan an account has is one function's to say,** `Plans.plan_for`, read with the request, never kept between requests (ADR 0020, revision "an account has a plan of its own"). `actio.identity` and `actio.usage` don't import each other: what a plan says of keys meets a key in `actio.api`.
- **A request that changes something, from a page of another site, is refused** (ADR 0009): a browser sends a cookie by itself, and never a key.
- **The API describes itself** (`/api/docs`), and its description can say how a key is sent, so that the page of docs can send one.
- **One person maintains it:** no new dependency, and no new setting.

## Options considered

**How a key is kept:**

1. **As it is.**
    - Pro: it could be shown again.
    - Con: a copy of the table is every key.
2. **As a fast hash, SHA-256.** Chosen.
    - Pro: found by its hash in one indexed read, as a session's token is.
    - Pro: a key drawn at random, of 256 bits, leaves nothing to guess faster: a slow hash protects what people choose, and nobody chooses a key.
    - Con: a key that people chose, or a short one, would be guessed from its hash. So nobody chooses one, and none is short.
3. **As a slow hash, as a password is (Argon2id).**
    - Pro: a weak key would be slow to guess from its hash.
    - Con: 60 ms and 64 MiB at every request ([ADR 0010](0010-accounts-with-a-password.md)), for a key that isn't weak.
    - Con: a salted hash can't be looked up by its value: the key would have to be found by another part of it first, and then checked.

**How a key is found:**

1. **By its hash,** unique in the table. Chosen.
    - Pro: one statement, which reads the key with its account's row, as a session's does.
2. **By its prefix, and the rest compared.**
    - Pro: works with a slow hash.
    - Con: the prefix has to be unique, and the comparison written to take the same time whatever it finds. With a fast hash, nothing is gained.

**What revoking is:**

1. **The row deleted.** Chosen.
    - Pro: nothing more is asked at each use, and a revoked key is answered as a key that never was.
    - Con: nothing is kept of it.
2. **The row marked.**
    - Pro: the list could say which keys there were, and when each was revoked.
    - Con: every use has one more thing to check, which a mistake could leave out, and the rows only grow.

**What a plan says:**

1. **Nothing: every account may make keys.**
    - Con: whoever runs an installation can't keep keys to the accounts they choose, and every account could make any number.
2. **Yes or no.**
    - Con: an account allowed keys could make any number of rows.
3. **A number, from 0 to 100.** Chosen.
    - Pro: says yes or no, and bounds the rows.

**What takes a key now:**

1. **Nothing.**
    - Con: a key that nothing takes can't be tried, and nothing shows that it is recognized as built.
2. **A route by which a key says what it is.** Chosen.
    - Pro: recognizing a key is built and tested whole, through the API and from `/api/docs`, and a key can do nothing else.
3. **Every route a session can call.**
    - Con: a key would make and revoke keys, change the password and read every conversation: one that leaked would be a session that never ends.

## Decision

**An account makes API keys in its session, and sees each one whole once. A key is kept as its SHA-256, found by it, and recognized in one place. Revoking deletes its row. The account's plan says how many keys it may have, asked when a key is made and at every use of one. One route takes a key: `GET /api/key`, which says what the key is.**

**The key** (`actio.identity.keys`):

- `act_`, then 8 characters, then `_`, then 43 characters: 56 in all. Both runs are digits and ASCII letters, 62 of them, drawn with `secrets`. The 43 are a little over 256 bits. With no `-` or other sign in it, a double-click selects the whole key.
- Its first 12 characters, `act_` and the 8, are its **prefix**: what a list shows, so that whoever holds a key can tell which row it is. It is no secret, and need not be unique.
- A text that isn't of this form is no key: it is answered without asking the database.

**The table,** `api_keys` (migration 0016): `id`, `user_id`, deleted with its account and indexed, `name`, `prefix`, `key_hash`, the SHA-256 of the whole key, 32 bytes, unique, `created_at`, and `last_used_at`, empty until the key is used. No column holds the key or its 43 characters, and none holds scopes, an expiry or a revocation.

**The rules,** in `actio.identity.keys`, which knows nothing of HTTP or the plans, never commits, and takes the time as `now`:

- **making a key** holds the account's row until the transaction ends, counts its keys, and refuses when it has as many as it may have, as its caller says. Two keys made at once are counted one after the other, and the second sees the first;
- **an account's keys** are listed oldest first;
- **revoking** deletes the row, by the key's id and the account's id in the statement itself, and says whether there was one;
- **recognizing** a text at `now` is `None` for a text that isn't of a key's form, with no query, and for one no row has. Otherwise it is who holds the key, from one statement that reads the key with its account's row. `last_used_at` is written at most once a minute, as a session's `last_seen_at` is. A key is an account's: no route gives a guest one, and a key that a guest's row was given by hand is nobody's, answered as one that isn't known.

**What a plan says,** `api_keys` in a `[plans.<name>]` table: how many keys an account on the plan may have at once, a whole number from 0 to 100. Left out, or 0: none. Unlike the plan's three limits, leaving it out gives nothing. The shipped plans file and the example's give `dev` 10, and `guest` and `free` none. A guest never has a key, whatever its plan says: that rule is the routes', not the file's, so a file with one plan for guests and accounts is still right.

- **The plan is asked when a key is made, when the keys are listed, and each time a key is used,** through `Plans.plan_for` and nothing else (`keys_allowed` in `actio.api.dependencies`). So the keys of an account taken off its plan stop working at their next request, are still listed and can be revoked, and work again when it is put back. An account on a plan the file doesn't have has the accounts' plan here too.

**The routes** (`actio.api.keys`):

| Route | Takes | Answers |
|---|---|---|
| `POST /api/keys` (`make_key`) | a session; a body with `name`, 1 to 64 characters without the spaces around them | 201, with the key's row and `key`, the whole key, this once |
| `GET /api/keys` (`list_keys`) | a session | `keys`, oldest first, and `at_most`, how many the account's plan allows: 0 for a guest and for a plan with none |
| `DELETE /api/keys/{key_id}` (`revoke_key`) | a session | 204; 404 for a key that isn't the account's, or isn't there |
| `GET /api/key` (`get_key`) | a key | what the key is (its id, name, prefix, `created_at` and `last_used_at`), whose (the account's id and address), and on which plan (its id and title) |

- **The first three take a session, never a key:** a key can't make, list or revoke keys. Two keys may have the same name.
- **Making a key refuses,** in this order and before anything is written: a guest, 403; an account whose plan allows none, 403, naming the plan by its title; an account that has as many keys as its plan allows, or more, 409, with the number and the plan's title.
- **A key is recognized in one place,** `CurrentKey` in `actio.api.dependencies`, beside `CurrentPrincipal`: the headers, then recognizing, then the plan, then the commit that keeps `last_used_at`. The key is taken from `x-api-key` or from `Authorization: Bearer`, the scheme in any case of its letters, without the spaces around it in either. Both there with the same key is that key.
- **`GET /api/key` refuses:** no key, 401; a text that isn't a key's form, or one no row has, 401, in the same words, since it may be mistyped or revoked; two different keys in the two headers, 401, saying so, though one of them works; a key whose account's plan allows none, 403, naming the plan. Each 401 carries `WWW-Authenticate: Bearer`. A session's cookie on the same request is not looked at: with a cookie and no key, the answer is 401.
- **No answer repeats a key that was sent.** The one member that holds a whole key, `key` in the answer of `POST /api/keys`, says that this answer is the only one that ever holds it.
- **The API's description** says both ways to send a key, as security schemes, so that `/api/docs` offers to send one ("Authorize"), and its introduction says what a key is, beside sessions.

**No whole key is written in any file of the repository,** made up or real: a test makes its keys when it runs, and a document writes `act_…`. The commit hooks scan for secrets, and refuse a key in some places and not in others (below, under "Measured"): the rule is the repository's own, and no hook holds all of it.

**Tests:**

- **the rules** (`apps/api/tests/test_keys.py`): a key's form, written in the test apart from the module's, and two keys that differ; its row holding its SHA-256 and its prefix, and no column the key or its last 43 characters; a key recognized with its account and its own plan; a key no row has, and texts of no key's form answered on a session that has no database behind it; a key on a guest's row, written by hand, recognized as nobody; `last_used_at` written at most once a minute; the number an account is told it may have, and fewer, as after a plan was lowered, with another account's keys not counted; an account's keys alone, oldest by when each was made; revoking by the account's id, and twice; the database refusing a second row with one hash, and deleting an account's keys with it;
- **the routes** (`apps/api/tests/test_keys_api.py`): a key made and shown whole once, never in the list; a name of 1 to 64 characters; a key recognized by each header alone, by `bearer` in lower case and in capitals, with spaces around it, beside another scheme, and in both headers; two different keys refused, whichever works; texts of no key's form refused by an app that has no database; a revoked key answered in the words of one that never was; another account's key left alone; a cookie that is no key, and a key that is no session, at each of the three routes; a key that works after a change of password and after a log-out everywhere; `WWW-Authenticate` on each 401, and no refusal holding the key that was sent; a guest, whose plan allows two here, refused and told 0, and a key given to a guest's row by hand answered as one that isn't known; a plan with none; the plan's number, reached and passed, and one key said as one; an account taken off its plan and put back, and one on a plan the file doesn't have; two keys made at once, one short of the plan's number, while the test holds the account's row: one made and one refused; `last_used_at` through the list, by the test's clock; and the description, with its two schemes;
- **the plans file's member** (`apps/api/tests/test_plans.py`): left out it is 0; 0, 1 and 100 are taken; −1, 101 and a word are refused, in one line; and the shipped files give `dev` 10, and the others none.

## Measured

On 2026-11-28, on the development machine, against the stack as `make up` runs it, through the proxy, with `curl`.

- **A request with a key costs what one with a session does.** 200 requests of each, one after the other and taking turns:

    | Request | Median | 95th percentile |
    |---|---|---|
    | `GET /api/me`, by a session's cookie | 4.6 ms | 6.0 ms |
    | `GET /api/key`, by a key | 4.6 ms | 6.1 ms |

- **A refusal costs no more.** Measured the same way:

    | Request | Median | 95th percentile |
    |---|---|---|
    | `GET /api/me`, with a cookie no session has | 4.8 ms | 6.1 ms |
    | `GET /api/key`, with a key of a key's form that no row has | 4.8 ms | 6.2 ms |
    | `GET /api/key`, with a text that isn't of a key's form | 3.1 ms | 4.0 ms |

    So whoever tries keys costs the server what whoever tries cookies does, a hash and one indexed read, and less for a text that isn't of a key's form, which asks the database nothing. Nothing counts either.

- **On the running stack,** with an account put on `dev` by `make plan`:
    - a key was made, of 56 characters in the form above. Its row's `key_hash` was the key's SHA-256, worked out apart with `shasum`, and the row, read whole, held neither the key nor its last 43 characters;
    - the key was recognized by each header, and by both at once;
    - no key, a cookie alone, a text of no key's form, a key no row has and two different keys were each answered 401, with `WWW-Authenticate: Bearer` and in the words above, and no refusal's body held the key that was sent;
    - the key alone was answered 401 at the three routes that take a session, and at `GET /api/me`;
    - with the account taken off `dev`, the key was answered 403 at its next request, and was still listed, with `at_most` 0. Put back, it was answered 200;
    - revoked, it was answered 401, and a second revoke 404;
    - the logs of the API, the worker and the proxy over that time, 1,076 lines, held neither the key nor its prefix.
- **From `/api/docs`, in Chromium, logged in to the account:** `POST /api/keys` made a key with the browser's session. Before "Authorize", `GET /api/key` went out with neither header, and was answered 401. With the key in the dialog's first box it went out as `Authorization`, and was answered 200; in the second, as `x-api-key`, 200. After the key was revoked, 401.
- **The commands of the "API keys" section of the getting-started page** were run as they are written there, from signing up to revoking the key.
- **What the commit hooks catch of a key,** tried with the hooks' own gitleaks, 8.30.1, on a made-up key of this form, in a folder outside the repository:
    - refused after `curl --header "Authorization: Bearer …"`, by its rule `curl-auth-header`;
    - let through as an assignment in Python, in a JSON answer, and after `x-api-key:`;
    - `act_…` and a prefix alone are let through, as they should be.
- **Fifty-eight mistakes were planted,** one at a time, in a scratch copy of the tree: in a key's form and in what is kept of it, in when it was last used, in whose keys are counted, listed and revoked, in the hold on the account's row, in what a guest's row may have, in each refusal and its header, in how a key is sent, in the plans file's member and the shipped files, in the migration and in the description. Each fails a test. Of the fifty-six planted before the review, two had failed none: every account's keys counted against one account's number, and one key said as "1 API keys". Each got a test.

## Consequences

- **Easier:**
    - a program can say which account it works for, with a key its owner made, listed and can revoke;
    - a stolen copy of the database gives no key, as it gives no session;
    - a route that takes a key asks for `CurrentKey` in one line, and gets the 401 and the 403 in the shared format, with the plan asked;
    - whoever runs an installation decides who may have keys, and how many, in the plans file and with `actio plan`.
- **Harder:**
    - **a key is shown once.** Lost, it can't be read again: its owner revokes it and makes another;
    - **each use of a key is one indexed read,** and a write at most once a minute, as a session's is.
- **What it leaves:**
    - **no scopes:** a key can do one thing, so there is nothing to tell apart;
    - **no expiry:** a key works until it is revoked, or its account's plan allows none;
    - **no page:** keys are made, listed and revoked through the API, or from `/api/docs`;
    - **a key reads nothing of its account but what it is;**
    - **no limit of its own for a key:** nothing counts how often keys are made or tried. The plan's number bounds the rows, and a key of 256 bits isn't guessed: a session's cookie has no such limit either;
    - **a key outlives a change of password and a log-out everywhere,** and is revoked by itself;
    - **making one asks no password,** so whoever holds a session can make one;
    - **nothing is kept of a revoked key,** nor of what a key was used for beyond when it was last used.
- **How to reverse it:** routes that take a key depend on `CurrentKey`, as routes that take a session depend on `CurrentPrincipal`. Keys kept another way change `actio.identity.keys` and a column, with a migration; revoking by a mark is a column and one more condition in `recognize`. Keys taken out altogether are `actio.api.keys`, `CurrentKey` with `NO_KEY` and `keys_allowed`, `actio.identity.keys`, `ApiKey` and a migration that drops the table, which deletes every key, and `api_keys` in the plans files and `PlanEntry`, with their tests, then `make openapi`.
