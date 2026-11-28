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

## Revisions

### 2026-11-28: an account's keys in its place on the page

"No page", under "What it leaves", is done: the account's place on the page, `/account` ([ADR 0019](0019-accounts-in-the-one-screen.md)), lists the account's keys, makes one and shows it whole once, and revokes one, through the three routes that take its session. The API didn't change.

**Why now, and not with the routes.** The decision built and measured the rules, the routes and the description whole first, and left a key to be made with `curl` or from `/api/docs`. Whoever an installation lets have keys is a person at a browser, logged in already, who needed a shell or the API's own page of docs for what is a box and three buttons.

**The decision: a last section of the account's place, which shows the keys as the API last listed them, makes a key and shows it in one selected box once, and revokes one after asking.** In `apps/web`: `components/account/api-keys.tsx`, and `lib/api-keys.ts` for what is read.

- **Where it is: last, after "Log out", headed "API keys".** Its list comes after the place is drawn, and a key just made and the form change its height. Last, nothing below it moves; above "Log out", it would move those buttons under the reader.
- **What it reads:** `GET /api/keys`, held under the session it was read in, as everything the page reads is (ADR 0019), under `["keys", holder]`. Only the account's own part of the place reads it: a guest at `/account` asks nothing, and nothing is asked before the page knows whose session it has. A reading that fails is said, with "Try again", and is never tried again behind the reader's back; the keys are read again whenever the window is returned to, which is how a key used meanwhile, or one made or revoked in another window, comes in.
- **The server is the truth about the keys,** as about the list of conversations ([ADR 0018](0018-the-list-of-conversations-is-the-servers.md)): the page shows them as the API last listed them, in its order, and inserts, takes out or sorts nothing. After a making and after a revoking, whatever was answered, they are read again. A refusal says that the list was out of date; an answer that never arrived may have made a key all the same, which the list then shows, to be revoked, or revoked one, which the list then no longer shows.
- **What the plan allows is the API's `at_most`,** said in words under the heading, after "An API key lets a program use Actio as this account.": "Your plan allows 2 at once."; "Your plan allows none."; and with keys still listed, as after a plan was lowered to none, "Your plan allows none now: the keys below are refused until it does." Where the form stands, the form while the account has fewer keys than that; at the number "You have as many keys as your plan allows. Revoke one to make another."; over it "You have more keys than your plan allows. Revoke some to make another."; and with a plan that allows none, nothing. The section doesn't name the plan: the usage meter does.
- **The list:** each key by its name, its prefix and `…`, in the page's monospace, when it was made and when it was last used, or "Never used", and a button "Revoke", named "Revoke “…”" for a screen reader, as a conversation's delete button is. A name of 64 characters without a space folds inside its row. A moment is said by a new function of `lib/time.ts`, `onDay`, by the reader's clock: "today at 2:30 PM", in the reader's hours as `atTime` says them, and any other day "on Nov 16, 2026", with its year and without a time, since a key may be months old. The list reads the clock once a minute, as the usage meter does.
- **The form:** one box, "Name", with what it is for under it, and "Make a key", "Making…" while it is sent, once however often it is pressed. The name goes without the spaces around it, and is held to the API's 1 to 64 characters first (`KEY_NAME_MIN` and `KEY_NAME_MAX` in `lib/account.ts`, held to the API's description by a test): none is "Give the key a name.", with nothing sent. The box is no password's and no address's (`autoComplete="off"`). A 422 that names `name` is "Use 1 to 64 characters." under the box, and the 403 and the 409 are the API's own words there, which name the plan; the keyboard goes on the box. Anything else is said in the section's notice.
- **A key just made stands where the form was,** from the answer of `POST /api/keys`, whatever the reading after it says: one read-only box, named "Your new key, “…”" and described by "Copy it now, and keep it as you keep a password: it isn’t shown again.", with "Copy" and "Done".
    - **All of it can be read at once:** a `textarea` of two rows, in the monospace, that breaks anywhere, so that the 56 characters stand whole in it in a window 320 px wide. The box is as high as what is in it (`field-sizing: content`): with letters larger than the browser's own, the key takes a third row there, and none of it is out of sight (below, "Measured"). The browser is told to leave it alone: no spelling, no filling in, no correcting and no capitals.
    - **When it appears, the keyboard is put on it, and all of it is selected.** So a copy by the keyboard works at once, in every browser and at every address (below, "Measured").
    - **"Copy" writes it with `navigator.clipboard.writeText`,** and a line under the buttons says "Copied.", with the keyboard put on the button by the page: WebKit puts it on no button that is clicked. Where the page has no clipboard, as at a plain http address other than localhost, or the browser refuses, the keyboard is put on the box with all of it selected again, and the line says "It couldn’t be copied for you. It is selected: copy it with your keyboard." The line is a `role="status"`, on the page and empty from the key's first draw. `document.execCommand("copy")`, which browsers keep for old pages, is not used: the selection is there for the keyboard already.
    - **"Done" takes it off the page,** its box and the state that held it.
    - **It is held in the state of the component that shows it, and nowhere else:** never in the cache of what the page read, the browser's storage, the address, the title, an attribute or the console. The section is drawn anew for each session, so the key is also gone when the account's place is left and when the session ends.
- **Revoking asks first, in the row's place,** as deleting a conversation does: a group named "Revoke “…”?", the key's name, "Revoke this key? Whatever uses it is refused from then on, and it can’t be brought back.", "Cancel", which takes the keyboard, and "Revoke", "Revoking…" while it is sent, the filled one, with its line where a system's own colours take the place of the page's. Escape is Cancel, and goes no further: around it, Escape puts away the list of conversations over a narrow window. Confirmed, the key is revoked once, the keys are read again whatever was answered, and its row goes when that reading says so, not before. A 404 is a key revoked already, in another window: done, and nothing is said. After any other failure, a key the reading no longer lists is revoked all the same, its answer lost on the way, and nothing is said of it; one it still lists stays, and the failure is said in the section's notice.
- **The section's notice** says what went wrong with the last making or revoking, or else with the last reading, with "Start a new session" for a session that ended, and otherwise "Try again" while the keys couldn't be read. One notice: a making refused because the session ended is followed by a reading refused for the same reason, which would say it twice.
- **Where the keyboard goes,** put there by the page each time, and never left to a click: on the key's box when it is made, and on the name's box when it wasn't, refused or failed; on "Copy" once it has copied; after "Done", on the name's box where the form is then, and otherwise on the section's heading, which can take it; on the heading too when the form gives way under the keyboard to a sentence or to nothing, as after a refusal whose reading says the plan's number is reached, and never when the keyboard is elsewhere by then; on the heading after a "Try again" that read the keys, whose button goes with its notice; after Cancel, Escape or a revoking that left the key listed, back on that key's "Revoke"; after a revoking, on the "Revoke" of the key listed after it, or else of the one before it, while the list still has that one and its own question isn't open, and otherwise on the heading.
- **The end-to-end stack's plan for accounts allows two keys** (`deploy/e2e/config/plans.toml`), and its plan for guests none; the shipped files still give accounts none. ADR 0020's revision "a plan says how many API keys its accounts may have" said that the stack's file said nothing of keys: from this revision on, it does.

**Not chosen:**

- **a dialog over the page for the key just made:** one that holds the keyboard is written here or not at all, as ADR 0018 and ADR 0019 found, and the form's place holds the key as well;
- **a box of one line:** in a narrow window the key would scroll sideways inside it, and part of it would be out of sight to whoever copies it by reading;
- **the key that was made put into the list at once:** the list would show a row the server hasn't listed, and a refusal or an answer that never arrived would need the page to work out what to take out again.

**Tests:**

- **the section** (`components/account/api-keys.test.tsx`), through the whole page against the fake API, which answers an account's `GET /api/keys` with no keys and a plan that allows none, as the shipped plans do, and draws each key it makes as the test runs: no section and no reading for a guest, nor before the session is known; the section last, after "Log out"; "Reading your keys…" until they are read; each sentence of what the plan allows, and the form, or the sentence where it stands; a row's words by a clock the test sets, today and another year, in the API's order, which sorting by name or by time would change; a key made, its name sent without its spaces, shown in its box with the keyboard on it and all of it selected, and the keys read again; the key shown while that reading is still under way, and no row the reading doesn't list; the key once in the page as it is written, in its box, and in neither storage, the address, the title nor an attribute, and nowhere after "Done"; where the keyboard goes after "Done", by what the reading said meanwhile; the key not shown again after the place was left; "Copy" with a clipboard, also by a click that puts the keyboard nowhere, without one, and with one that refuses; a name of nothing and of spaces, and the box's length; each refusal under the box; one making however often the button is pressed; a failure in the notice, with the keyboard on the name's box, and a session that ended in one; the key shown though the reading after it failed; the form giving way under the keyboard, to a sentence and to nothing, and leaving the keyboard alone when it is elsewhere; revoking, asked and not, by Cancel and by Escape, which leaves the list over a narrow window where it is; a row kept until the reading says it is gone, and the keyboard after it, on the next, the one before and the heading, also when the next key's own question is open; a 404 said as nothing; another failure, after which the keys are read again, and a key they no longer list taken for revoked; a reading that fails after a revoking, which leaves what was read before, the row with it; a first reading that failed, and "Try again", which leaves the keyboard on the heading; and the next session's keys read anew, with nothing of the last session's shown meanwhile;
- **the rules:** `onDay` (`lib/time.test.ts`), `keyNameProblem` (`lib/account.test.ts`), and the two numbers against the API's description (`tests/contract/limits.test.ts`);
- **end to end** (`e2e/keys.spec.ts`), each test under an account of its own, signed up before any question is asked: the plan's two keys said; a key made, of a key's form, in its box with the keyboard on it and all of it selected; "Copy", with the clipboard granted to the browser, putting that key on it; all of the key in sight in a window 320 px wide with letters half again as large as the browser's own; the key the page showed answered 200 by `GET /api/key` to a request of the test's own, with no cookie, with the key's name and the account's address; the key nowhere in the page after "Done", and the list naming it by its name and its first twelve characters, before and after a reload; and a second key reaching the plan's number, the sentence where the form was, and a key revoked through its question bringing the form back, and answered 401;
- **the stack's plans file** (`apps/api/tests/test_plans.py`): two keys for an account, none for a guest.

**What it leaves:**

- **a key can't be renamed;**
- **nothing says what a key was used for,** beyond when it was last used;
- **making a key asks no password,** as the decision left it: whoever holds a session can make one;
- **where a browser gives the page no clipboard,** as at a plain http address other than localhost, the key is selected for the keyboard to copy: "Copy" can't write it;
- **a key that is on the page when the browser keeps the page in its back/forward cache** is there when the page comes back;
- **the section doesn't name the plan,** which the usage meter does.

**How to reverse it:** `ApiKeys` in `components/account/account.tsx`, with `components/account/api-keys.tsx` and its test; `lib/api-keys.ts`; `listKeys`, `makeKey`, `revokeKey` and their types in `lib/api/client.ts`; `KEY_NAME_MIN`, `KEY_NAME_MAX` and `keyNameProblem` in `lib/account.ts`, and `onDay` in `lib/time.ts`, with their tests; the fake API's answer to `GET /api/keys` and its makers of keys; `e2e/keys.spec.ts`, and `api_keys` in the stack's plans file with its test. The API stays as it is.

#### Measured

Before the build, on 2026-11-28, with the repository's Playwright 1.65, in Chromium, Firefox and WebKit, on a page with a read-only `textarea` of 56 characters and a button:

- **At `http://localhost:8090`, a secure context,** `navigator.clipboard.writeText` from a click wrote in Firefox and WebKit. In headless Chromium it was refused (`NotAllowedError: Write permission denied`), and wrote once the browser's context had been granted `clipboard-read` and `clipboard-write` for the page's origin, after which `navigator.clipboard.readText()` read the text back. Firefox and WebKit don't know those two permissions.
- **At `http://192.168.1.50:8080`,** plain http on a LAN address, as a self-hosted Actio may be opened: `window.isSecureContext` is false, and `navigator.clipboard` is `undefined` in all three.
- **In all three, at both addresses,** `box.focus(); box.select()` gave `selectionStart` 0, `selectionEnd` 56 and the box the keyboard, and a copy by the keyboard (`ControlOrMeta+C`) then put the whole text on the clipboard.

After the build, the same day, with the same Playwright and its three engines.

- **On the stack as `make up` runs it,** in Chromium, as an account made up for it:
    - on the plan for accounts, the section said that the plan allows none, with no form;
    - put on `dev` by `make plan`, and the page loaded again, it said 10 at once. "Log out" stood where it had stood before the keys were read: it had moved by 0 px;
    - a key was made, of 56 characters in the decision's form. Its box had the keyboard, with characters 0 to 56 selected. The key stood once in the page as it is written, and in neither storage, the address nor the title;
    - a copy by the keyboard, pasted into another page of the same browser, was the whole key;
    - a request of the probe's own, with the key as `Authorization: Bearer` and no cookie, was answered 200 by `GET /api/key`, with the key's name, the account's address and the plan `dev`;
    - after "Done" the page's content held the key nowhere, nor after a reload, when its row said "Last used today at 1:08 PM";
    - taken off `dev` with a key left, the section said that the plan allows none now, the key was listed, and `GET /api/key` answered it 403. Once it was revoked, the section said that the plan allows none.
- **On the end-to-end stack, in the three engines,** each under an account signed up for it, with real keys and clicks. Where the keyboard was, in all three:
    - on the name's box after a making whose request never arrived, and on the key's box once it was made, by Enter in the name's box;
    - on the name's box after "Done";
    - on the section's heading after a making that was refused at the plan's number, the second key having been made by another request meanwhile, when the form gave way; and after a "Try again" that read the keys;
    - on "Cancel" when a revoking asked; back on the key's "Revoke" after Escape, which left the list of conversations where it was; on the next key's "Revoke" after a revoking by the keyboard; and on the heading when the last key went.
- **"Copy", by the pointer:** Firefox and WebKit wrote the key and said "Copied.", with the keyboard on the button. Headless Chromium refused, as before the build, and the key was selected, with the other line. Before the page put the keyboard on the button itself, WebKit left it on nothing after that click: it puts the keyboard on no button that is clicked. It also goes to a button by Option and Tab, as Safari does unless it is told otherwise: Tab alone, from "Cancel", went past "Revoke".
- **A revoking whose answer was cut off on its way back,** the key revoked: its row went with the reading after it, and nothing was said.
- **The key's box,** at windows from 1,100 px down to 320 px wide:
    - the key stood in two rows at every width, in all three engines. At 320 px the box's text is 262 px wide, and a letter of the monospace 8.43 px in Chromium and Firefox and 8.65 px in WebKit: 31 and 30 letters a row, of the 28 that two rows take;
    - with the page's letters at 18, 20 and 24 px, for a browser's own 16, the key took a third row and more at 320 px. In a box two rows high it scrolled out of sight: at 20 px, 95 px of text in 70 px;
    - all three engines know `field-sizing: content`, as Playwright 1.65 ships them (Chromium 158, Firefox 159, WebKit 27.2). With it the box was three rows high at 20 px, four at 24 px, and five at 32 px, six in WebKit, with all of the key in sight each time;
    - a name of 64 letters without a space folded into four rows in its row at 320 px, with "Revoke" inside the row, and the page didn't scroll sideways.
- **Seen beside it, and older than the section:** at 320 px with letters at 24 px the page scrolls sideways by 26 to 37 px, a guest's chat too. The header's "New chat" reaches past the window's edge; nothing of the section does.
- **Playwright 1.65's Firefox died, by signal 11,** in each of the 30 runs in which the probe read all of several elements at once (`allInnerTexts`, `allTextContents`, `evaluateAll`) through a section found by its name, on the account's place, after a key had been made in that page. It didn't when the probe read one element, counted, clicked or typed; when a script in the page read every element's text; before a key was made in the page; on a page that only listed a key made through the API; nor on pages that are not Actio's with the same parts. Chromium and WebKit never did. The cause wasn't found. No test of the repository runs Firefox, and the look reads its texts in the page.
- **Mistakes were planted,** one at a time, in a scratch copy of the tree: 94 in the section, what it reads, the name's limits, `onDay`, the account's place and the stack's plans file, against the component tests and the plans' test, and 4 on a stack built from the copy, against the end-to-end spec. The tests of what the review and the look found were written before any planting.
    - Of the 94, three failed nothing. A plan that allows none given a sentence in the form's place was a gap: two tests got an assertion, and it fails both. The form's own guard against a second sending, taken out with its button still disabled while it is sent, is no mistake while the button is: with both taken out, a test fails. And the section not drawn anew for each session is what no test can see, as the rules say.
    - The 4 each fail the spec: a box two rows high whatever is in it, at the check with larger letters, which no other test holds; the key's prefix shown for the key; a plan of one key; and "Done" leaving the key on the page.
