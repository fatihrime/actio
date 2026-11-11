# 0010. Lasting accounts with an email address and a password

- **Status:** accepted
- **Date:** 2026-11-10

## Context

A guest is gone after a day ([ADR 0009](0009-sessions-in-the-database.md)). To keep conversations, and later to link a chat bot or create an API key, a person needs an account that lasts, and a way to prove it is theirs.

- **Actio is self-hosted.** It can't assume that the machine it runs on can send email, or reach the internet at all.
- **A password table is the most valuable thing to steal.** People reuse passwords, so a leak here would open accounts elsewhere.
- **A guest who decides to stay** shouldn't lose what they already have.

## Options considered

**How a person proves who they are:**
1. **An email address and a password.**
   - Pro: works offline, with nothing to configure.
   - Con: passwords are reused, guessed and forgotten, and Actio has to store something derived from them.
2. **A link or a code sent by email,** with no password.
   - Pro: nothing to steal from the database.
   - Con: every installation would need a mail server before anyone could log in.
3. **Logging in through GitHub or Google.**
   - Pro: no passwords at all, and two-factor login comes with the provider.
   - Con: needs the internet, and an application registered with each provider.
4. **Passkeys.**
   - Pro: the strongest of the four, and nothing reusable is stored.
   - Con: browsers allow them only on HTTPS or localhost, and they need more interface than a form.

**How the password is stored:**
1. **Argon2id.** Built to make each guess expensive in memory as well as in time, which blunts graphics cards.
2. **bcrypt.** Proven, but it ignores everything after the 72nd byte of a password, and costs an attacker little memory.
3. **PBKDF2.** Everywhere, but cheap to run on graphics cards.

## Decision

We chose **an email address and a password, stored as an Argon2id hash.** The other ways to log in can be added beside it later; none of them can be the only one on a machine with no mail and no internet.

- **Argon2id with the library's default parameters,** which are RFC 9106's second recommended profile: 64 MiB of memory and three passes. One check takes about 60 ms inside the API's container.
  - Hashing blocks, so it runs in a worker thread, and at most two run at once. Each takes 64 MiB, and a burst of logins must not exhaust the container's memory.
  - A hash names its own parameters. When they are raised, each account's hash is replaced the next time its owner logs in.
- **Passwords are 10 to 256 characters,** with no rules about which characters. Length is what makes a password hard to guess; the upper bound keeps one request from making the server hash megabytes.
- **One address is one account, however it is typed.** Addresses are stored in lower case, and the database refuses a second account with the same one.
- **A guest who signs up becomes the account.** It is the same row, so everything the guest owns stays theirs.
- **A new session whenever what its holder may do changes:** after signing up, logging in and changing the password. The token the browser held before stops working.
- **Changing the password logs every other browser out.** If the old password had leaked, whoever used it is out.
- **A wrong password and an unknown address get the same answer, after the same work.** Measured through the proxy: 61.1 ms and 61.3 ms, both `401` with the same text. So a login form can't be used to find out who has an account.
  - Signing up does reveal it: a second sign-up with the same address is refused. Without email there is no way to hide that and still tell the person why.
- **An account's session ends after 30 days without a request,** and each use renews it. The cookie itself lasts 400 days, the longest browsers allow, so it always outlives the session it carries.
- **The database keeps the two kinds apart.** A check constraint requires each row to be wholly a guest (it expires; no address, no password) or wholly an account (both, and no expiry), whatever code writes it.

**No email is sent.** So an address is not confirmed, and there is no "forgot my password" link.

## Consequences

- **Easier:**
  - a stolen copy of the database gives neither sessions (only token hashes) nor passwords (only Argon2id hashes);
  - Actio works on a machine with no network;
  - other ways to log in can be added without touching sessions: each ends by starting one.
- **Harder:**
  - a forgotten password can only be replaced by whoever runs the installation. A tool for that comes with the admin pages;
  - an address is a label, not proof that its owner signed up.
- **Not done yet:**
  - **nothing slows repeated login attempts.** Each one costs the server 60 ms and 64 MiB, which also makes the login route a cheap way to load it. Rate limits are the next step;
  - a second factor, passkeys, and logging in through another provider.
- **How to reverse it:** routes depend on a `Principal`, and every way of logging in ends in the same function that starts a session. Replacing passwords by another method changes `actio.identity.accounts` and its three routes.
