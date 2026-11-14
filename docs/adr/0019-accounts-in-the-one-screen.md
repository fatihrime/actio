# 0019. Signing up and logging in happen in the one screen, and the page forgets a session that has ended

- **Status:** accepted
- **Date:** 2026-11-14

## Context

The API has lasting accounts ([ADR 0010](0010-accounts-with-a-password.md)): sign up, log in, change the password, log out of one browser or of all. The page had none of it. Every visitor was a guest, whose conversations are gone after a day ([ADR 0009](0009-sessions-in-the-database.md)).

- **A guest who signs up is the same account afterwards,** and a guest who logs in takes their conversations along ([ADR 0010](0010-accounts-with-a-password.md)). The page should let that be felt: nothing lost, nothing to find again.
- **The app is one screen that never loads a page,** because a page load ends an answer that is being written ([ADR 0015](0015-the-web-chat-calls-the-api-from-the-browser.md)).
- **The page keeps what it has read from the API,** such as the list of conversations ([ADR 0018](0018-the-list-of-conversations-is-the-servers.md)). Until now a page had one session for its whole life. With logging in and out it has several.
- **The API answers a login the same way for an unknown address and for a wrong password,** so that nobody can ask it who has an account ([ADR 0010](0010-accounts-with-a-password.md)). A form must not give away what the API keeps to itself.
- **No email is sent.** There is no confirmation, and no way to reset a forgotten password by oneself.
- **New guests are limited per network address** ([ADR 0011](0011-rate-limits-as-token-buckets-in-valkey.md)). Someone who has an account must be able to reach it when that limit is used up.

## Options considered

**Where the forms are:**

1. **Pages of their own,** loaded like any web page. After a login the browser loads the chat.
   - Pro: a page has one session for its whole life. Nothing read for one session can show up in the next, by construction.
   - Con: a guest who signs up in the middle of a conversation leaves it for another page, and comes back through a reload.
2. **Dialogs over the chat.**
   - Con: a dialog that holds the keyboard is hard to get right, and would be written here ([ADR 0018](0018-the-list-of-conversations-is-the-servers.md) decided against one for the same reason).
3. **Places in the one screen:** `/login`, `/signup` and `/account`, where the chat otherwise is.
   - Pro: nothing loads. After signing up, the guest is back where they were.
   - Con: the page lives through a change of session, and has to let go of what it read for the one before.

**What the page does with what it has read, when the session changes:**

1. **Keep it, under the account's id.** Another account's data is never shown, and the same account's is reused.
   - Con: wrong. See "Measured": the same account can log in again in one page's life, and what was read earlier is out of date by then.
2. **Load the page again at every change of session.** That is option 1 above.
3. **Hold everything under the session it was read in, and drop it when that session ends.**

**Whether opening `/login` starts a guest session first,** like every other address:

1. **No.** The form would only ask who has the session.
   - Pro: no guest is made for nothing.
   - Con: two ways for the page to start, and every form would have to work in both.
2. **Yes.** A guest made for nothing is deleted by the login that follows.
   - Con: it uses up one of the network's new guests for the hour.

**How a password is typed when it is chosen:**

1. **Twice,** in two boxes that must agree.
2. **Once,** in a box that can show what was typed.

## Decision

**Three places in the one screen. The page changes whose session it shows only with what the API answered, and everything it reads is held under the session it was read in, and dropped with it.**

**The places** (`lib/location.ts`, `components/account/`):

- **`/login`, `/signup` and `/account` are shown where the chat is,** with the list of conversations still beside them. Each is also a page to the server, so a bookmark or a reload works.
- **The header says whose session it is.** For a guest: Sign up and Log in. For an account: its address, which leads to the account's place. Under the list, a guest is told that it is kept for a day.
- **After signing up or logging in, the page goes back to where the chat was left.** The conversation that was there is the account's now, either way.
- **A guest is not offered to log out.** It would delete the guest, with everything they have ([ADR 0009](0009-sessions-in-the-database.md)).

**The forms:**

- **Signing up checks what can be checked before sending:** that the address has the form of one, and the password's length, counted in characters as the API counts them. The API has the last word. An address that has an account is said so under the address, in the API's words.
- **Logging in checks nothing but that both boxes are filled.** Whatever is wrong, the answer is the API's one sentence. Here a `401` means that, and not that a session has ended.
- **A password is typed once,** and can be shown while it is typed. Typing it twice mostly tests patience; seeing it catches the slip.
- **What is wrong is said under the box it is about,** tied to the box for a screen reader, and the keyboard is put on the first such box. The browser's own bubbles are turned off: they differ between browsers, and can't be tested here.
- **A password manager is told what each box is:** the address as the user name, a current or a new password. The form that changes a password carries the address in a box nobody sees, so the manager knows whose password it is replacing.
- **Changing the password asks for the current one,** and says afterwards that every other browser was logged out ([ADR 0010](0010-accounts-with-a-password.md)).
- **The limits the forms hold people to are the API's.** A test fails when the two differ.

**The session on the page** (`lib/session.ts`):

- **Whose it is comes from the API alone.** After signing up or logging in, the page takes the API's answer. After logging out, it asks again, and until the answer is there it shows nobody: not the account that left, and nothing of it.
- **Everything read from the API is held under the session it was read in:** whose it is, and which of the page's sessions it is. The same account logging in again is another session, and reads everything anew.
- **What was held for a session is dropped when it ends,** from the page's memory too.
- **A form waits until it is known whose session this is.** A login sent while a guest session is still being started could be overtaken by it, and leave the browser with the guest's cookie.
- **Logging out starts over as a new guest,** in a new conversation. The account's address leaves the browser's history.

**Opening `/login` starts a guest session like any other address.** One way for the page to start is worth one guest of thirty an hour. And where no guest can be started, because the limit is used up or the answer never came, the forms are there all the same: logging in needs no session.

## Measured

On a MacBook Pro (M1 Pro, 16 GB), in a headless Chromium against the stack of `make e2e`, 2026-11-14. Times are from the click to the chat being on the page, the median of seven:

| | |
|---|---|
| Signing up | 113 ms |
| Logging in | 104 ms |
| Logging out, to the chat as a new guest | 56 ms |
| Scripts a first visit downloads | 236 kB, 3 kB more than before the forms |

About 60 ms of the first two is the password being hashed ([ADR 0010](0010-accounts-with-a-password.md)).

- **A defect found by the first run in a real browser.** An account logged out and, in the same page, back in. Meanwhile it had gained a conversation, brought along by a guest. The list showed what it had been before. It was held under the account's id, which was the same as before, and the library that holds it doesn't ask again for what it was told two seconds ago. A component test now does the same, and failed before the fix. That is why what is read is held under the session and not under the account.
- **Six mistakes were planted,** to see the tests fail: what was read kept under the account's id again, a session's owner kept while the next session starts, a form shown before the session is known, a refused login reported as a session that ended, the page not returning to where the chat was left, and a conversation that began on the page shown empty after a form had taken its place. Each failed the tests meant for it, and no other.
- **Every test in a real browser six times over, two at a time: 156 of 156 passed.**

## Consequences

- **Easier:**
  - a guest becomes an account without losing their place, and sees that nothing was lost;
  - nothing on the page decides who is logged in. It shows what the API said last;
  - a second session in a page's life starts as empty as the first.
- **Harder:**
  - **whatever the page reads for someone must be held under the session,** not under a fixed name or the account's id. One place hands out what to hold it under, and one place drops it; a new reader has to use them;
  - **an answer of the API that arrives after its session has ended** is still put away, under the session that is gone. It is never shown, and it is gone when the next session ends or the page is closed. Loading the page again at every change would rule even that out;
  - **the page learns late that its session ended elsewhere,** when the password was changed in another browser: at its next request. The chat then says so, and offers a new session.
- **Not done yet:**
  - a forgotten password. Without email, only whoever runs the installation can replace one, and the tool for that comes with the admin pages ([ADR 0010](0010-accounts-with-a-password.md));
  - changing the address, and deleting the account;
  - a second factor, passkeys, logging in through another provider ([ADR 0010](0010-accounts-with-a-password.md));
  - a list of the browsers an account is logged in from.
- **How to reverse it:** two functions decide what happens after a change of session, `enter` and `startOver` in `components/chat/chat-app.tsx`. Made to load the page instead, they give option 1, and holding things under the session becomes unnecessary. The forms themselves wouldn't change.
