# 0009. Sessions in the database, in a cookie bound to the host

- **Status:** accepted
- **Date:** 2026-11-10

## Context

Actio has to know who is asking. Usage limits are per person, conversations belong to someone, and later the chat bots and the developer API keys act for the same accounts.

- **The first visit should just work.** Someone who opens the page gets a guest account, with no form to fill in.
- **Access must end at once** when a session is ended: by logging out, by an administrator, or when limits are abused.
- **It must work on plain `http://localhost`** and on a home network, as well as behind HTTPS.
- One person maintains it, and the Python application owns the business logic ([ADR 0003](0003-web-app-on-nextjs.md)).

## Options considered

1. **Sessions kept on the server:** a random token in a cookie, and a row per session in Postgres.
   - Pro: ending a session is deleting a row. It takes effect on the next request.
   - Pro: nothing to sign or verify, and no keys to manage.
   - Con: every authenticated request reads one row.
2. **Signed tokens (JWT)** that carry the identity, with nothing stored.
   - Pro: no database read per request.
   - Con: a token can't be withdrawn before it expires, unless the server keeps a list of withdrawn tokens. That list is a session table again.
   - Con: signing keys to protect and rotate.
3. **An authentication library or service:** Auth.js or Better Auth in the web app, or a separate server such as Keycloak.
   - Pro: social login and email flows come ready-made.
   - Con: identity would live in the web app or in another service, while the API, the bots and the API keys all need it.
   - Con: one more moving part on a 16 GB laptop.

## Decision

We chose option 1.

- **The token is 32 random bytes,** shown once: it goes into the browser's cookie. The database keeps only its SHA-256, so a copy of the `sessions` table can't be used to log in.
  - A fast hash is enough here, unlike for passwords: a random token leaves nothing to guess.
- **The cookie** is `HttpOnly`, so page scripts can't read it; `SameSite=Lax`; and has no `Domain`, so only this host receives it.
  - Over HTTPS it is also `Secure`, and named `__Host-actio_session`. With that prefix a browser accepts it only from this exact host, over HTTPS, for the whole site.
  - Over plain HTTP it is `actio_session`, without either: browsers refuse `Secure` cookies from plain HTTP sites other than localhost, and the prefix needs HTTPS.
  - `ACTIO_PUBLIC_URL`, the address people open, decides which of the two applies.
- **Requests from other sites' pages are refused,** when they would change something. A browser attaches cookies whichever page triggers a request, so another website could otherwise act as its visitor (cross-site request forgery).
  - The browser says where a request comes from, in headers a page can't forge. `Sec-Fetch-Site` must be `same-origin` or `none`. Where it is missing, `Origin` must be Actio's own address. A request with neither doesn't come from a browser, so no cookie rides along unasked, and it passes.
  - A sibling subdomain (`same-site`) is refused too: it is not this application.
  - This replaces the classic hidden form token. The client needs no extra plumbing, and the rule sits in one place in front of every route.
  - `SameSite=Lax` is a second barrier, should the first ever be misconfigured.
- **A guest is a real account** that lasts 24 hours, with one session. Ending that session deletes the guest and what it owns: nothing could ever log in to it again.
- **Row ids are UUID version 7,** made by the application. They start with the time, so new rows land together in an index.
- **Errors use one format,** RFC 9457 problem details, whichever part of the application raises them.
- **Two modules, one boundary.** `actio.identity` holds the tables and the rules and knows nothing about HTTP. `actio.api` holds the routes, the cookie and the cross-site check. The import rules keep the web framework out of the first.
- **Time is passed in.** Nothing in `actio.identity` reads the clock, so tests move time by hand: a session is alive one microsecond before 24 hours and gone at 24 hours.

## Consequences

- **Easier:**
  - logging out, and later "log out everywhere" or banning an account, is deleting rows;
  - bots and API keys can point at the same `users` table;
  - a new route asks for "who is asking" in one line, and gets a 401 in the shared format without one.
- **Harder:**
  - each authenticated request costs one indexed read. The session's "last seen" time is written at most once a minute, so reading doesn't become writing;
  - on plain HTTP the cookie travels unencrypted, like everything else there. Serving Actio to others needs HTTPS.
- **Not done yet:**
  - **limits on creating guests.** Anyone who can reach the API can create them, and expired ones are not cleaned up yet. Rate limits and the cleanup job come in later steps; until then this is for a machine you control;
  - **email and password,** which come next and turn a guest into a lasting account;
  - **workspaces.** Every row belongs to the one deployment. Adding a workspace column later is one migration per table.
- **How to reverse it:** routes depend on a `Principal`, not on how it was established. Replacing the session lookup with another scheme changes one dependency in `actio.api`.

## Revisions

### 2026-11-10: sessions of lasting accounts

Email and password arrived with [ADR 0010](0010-accounts-with-a-password.md). For sessions, that adds two rules to the ones above:

- **An account's session ends after 30 days without a request,** and each use renews it. A guest's session still ends with the guest, used or not.
- **A new session starts whenever what its holder may do changes:** on signing up, logging in and changing the password. The old token stops working.
