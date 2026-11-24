# Performance

A chat page is judged in its first second and in every frame after it: how soon the box can be typed into, whether the text stutters while an answer arrives, whether a long conversation opens at once. Actio holds itself to numbers for these, measures them at the end of every phase and after every large change to the page, and fixes what a run finds before the next phase starts.

This page says how a run is made, which numbers it holds, what the last run measured, and what the runs have found so far.

## How a run is made

- **The stack is the production build,** in containers, as `make up` runs it, with the fake model in place of a real one ([fakellm](reference/fakellm.md)). A proxy in front of the fake model lets an answer through at a chosen speed, a word at a time. So a run measures the page and the API, and not how fast a model writes.
- **Three browser engines:** Chromium, Firefox and WebKit, which is Safari's, driven by Playwright. Several faults showed in one engine only.
- **Frames are the page's own:** the time between two animation frames, noted inside the page. An input is timed from its event to the frame that shows it.
- **A slower device is Chromium with its CPU slowed four times,** and a phone is that on a slow 4G connection. Firefox and WebKit can't be slowed that way, so they are measured at the machine's own speed.
- **Builds are compared back to back,** the same night: the build before a change and the build after it, swapped several times. One run on its own says little, since the machine is never doing nothing else.
- **A figure says what it is:** the middle value of several runs, or the range from the least to the most of them.

The probes, and the proxy that paces the fake model, are scripts kept outside the repository for now. The figures below were taken on an Apple M1 Pro with 16 GB.

## The numbers Actio holds itself to

- **The first visit.**
    - On a desktop the box can be typed into within 0.3 s.
    - On the phone profile it can be typed into within 2.0 s, and the largest paint comes within 2.5 s.
    - Nothing on the page moves as it loads, or when a stored conversation opens: a layout shift under 0.05.
- **An answer as it arrives,** up to 4,000 words at 300 words a second.
    - No frame takes longer than 50 ms, in all three engines.
    - With the CPU slowed four times, none takes longer than 250 ms.
- **Typing and Stop.**
    - 95 of 100 keys are shown within 50 ms, under a long conversation and beside an arriving answer too.
    - After Stop, the box can send again within 100 ms.
- **Long conversations and lists.**
    - A conversation of 100 exchanges opened from the list: no frame longer than 100 ms, in all three engines.
    - A list of 300 conversations is shown within 150 ms.
    - 600 messages are scrolled without a frame over 50 ms.
    - The list put away or shown again beside a long conversation takes one frame.
    - A conversation that its reader left away from its end opens at that place again, to within 2 px, from the first frame that shows it, also after the page was loaded again in the same tab.
    - From anywhere in a conversation of 300 exchanges, going to its newest message takes no frame longer than 100 ms, in all three engines.
- **After forty rounds of use** the page holds no more elements and listeners than after the first.
- **The API.**
    - Its routes answer within 20 ms for 95 of 100 calls.
    - When 50 answers are started at once, the first word of each comes within 1 s, and none is refused or lost.
    - A conversation of 600 messages is read whole with a middle value within 50 ms.

## The last run: the end of Phase 1

Measured on 2026-11-23 and 2026-11-24, with Chromium 158, Firefox 159 and WebKit 27.2.

| | Measured |
|---|---|
| First visit on a desktop, until the box can be typed into | 0.11 s in Chromium, 0.08 to 0.11 s in Firefox, 0.10 to 0.11 s in WebKit |
| First visit on the phone profile | the box after 1.9 s, the largest paint after 2.1 s |
| Transferred on a first visit | 0.5 MB, in 20 requests |
| Layout shift as the page loads, and when a stored conversation opens | under 0.002, and 0.008 to 0.018 |
| The longest frame while 4,000 words arrive at 300 a second | 17 ms in Chromium, 9 to 41 ms in Firefox; in WebKit 31 to 54 ms over twelve runs on a machine busy with other work, 41 ms in the middle |
| The same with the CPU slowed four times, in Chromium | 33 ms |
| A key typed beside an arriving answer, until it is shown, 95 of 100 | within 17 to 19 ms in Chromium, 6 to 7 ms in Firefox, 17 ms in WebKit |
| A key typed under a conversation of 300 exchanges, 95 of 100 | within 22 ms in Chromium, 8 ms in Firefox, 17 ms in WebKit |
| After Stop, until the box can send again | 43 ms |
| A stored conversation of 100 exchanges opened by its address, until its end is on the page | 0.16 s in Chromium and WebKit, 0.25 s in Firefox |
| The same opened from the list: the longest frame | 33 ms in Chromium, 83 ms in Firefox, 79 ms in WebKit |
| 600 messages scrolled through: the longest frame | 17 ms in Chromium and in Firefox, 126 ms in WebKit |
| The list put away and shown again beside 300 exchanges: the longest frame | 17 ms, and 50 ms at the most the first time |
| A list of 300 conversations | shown after 119 ms |
| Forty rounds of use | flat: 214 elements and 371 listeners after each |
| The API's routes, 95 of 100 calls | within 4 to 9 ms |
| 50 answers started at once: the first word, 95 of 100 | within 0.61 to 0.68 s, none refused |
| A conversation of 600 messages read whole | 37 to 38 ms in the middle |
| Memory of the whole stack at rest | 373 MiB, in six containers |

Two of these are not within their numbers, both in WebKit.

- **The longest frame while an answer arrives** is the one in which the answer ends, which draws all of its words once more. The night before, on a quieter machine, that frame took 26 to 35 ms on a build that drew nothing else in it, as the page now does again. On the busy machine these twelve runs were made on, one of them took 54 ms.
- **Scrolling through 600 messages** has one frame of 126 ms in WebKit, and had one of 114 ms at the run before. It is known and not yet looked into.

## What the runs have found

Each of these was a fault nobody had seen by using the page, and each is recorded where its decision is.

- **The first block of code in a language stopped the page:** for 1.5 s in Safari's engine with a block of TypeScript, 0.12 s in Chromium, while the language's patterns were made ready. Code is now coloured in a worker ([ADR 0016](adr/0016-answers-shown-as-markdown.md), revision "code is coloured in a worker").
- **An arriving answer was drawn again at every frame,** 57 to 110 times a second, its whole markdown each time. While 1,500 lines of code arrived, Firefox had 189 frames longer than 50 ms. An arriving answer is now handed to the renderer at most every 50 ms (ADR 0016, revision "an arriving answer is drawn at most every 50 ms").
- **A stored conversation opened as one uninterrupted render:** 183 ms without a frame for 100 exchanges in Chromium, about 250 ms in Firefox and in Safari's engine, and 350 to 570 ms for 300. It now comes onto the page newest first, twenty messages at a step ([ADR 0015](adr/0015-the-web-chat-calls-the-api-from-the-browser.md), revision "a long conversation opens newest first").
- **The page's frame moved when the session arrived:** a layout shift of 0.19. What arrives with the session now fills a place that was already there (ADR 0015, revision "the page's frame is there before the session").
- **A script in the document's head held back the page's own scripts** on a slow connection. It stands at the start of the body now, and on the phone profile the box can be typed into after 1.86 s where it took 2.02 s (ADR 0015, revision "the frame's script stands at the start of the body").
- **The line under an answer cost Safari's engine a long frame** at the end of a long answer: 37 to 54 ms where 50 is the limit. The line now comes one frame after the answer's last words (ADR 0015, revision "what an answer took comes a frame after its last words").
- **Reading a conversation read it again at every step.** The query that walked a conversation's branch read 360,000 rows for 600 messages, for whoever opened the conversation and before every answer in it. It was found because one timing had two values from run to run. The branch is now picked out of one read ([ADR 0012](adr/0012-conversations-as-a-tree-of-messages.md), revision "a branch is picked out of its conversation's messages"): a conversation of 600 messages is read in 37 to 38 ms where it took up to 93, and the first word of an answer after 300 exchanges comes after 45 to 47 ms where it took up to 103.

Measuring a new piece finds things too. Making a conversation open where its reader had left it (ADR 0015, revisions "a conversation opens where its reader left it" and "fewer messages at once, the end without a wait, the place kept over a reload") found two:

- **The page jumped under a reader going up through code,** in Safari's engine and in Firefox: by 134 and by 85 px each time a block of code came into view, because the markdown library draws a block that is off the screen as a placeholder. Chromium keeps a reader's place itself and hid it. A block of code has its real height from the first layout now (ADR 0016, revision "a block of code has its real height from the first"), and a long conversation takes a fifth longer to be whole behind its first screen.
- **A press at the wrong moment took messages off the page again.** Going to the newest message right after coming back far up a long conversation, in Firefox, once in 42 presses: 236 of 298 messages were shown, gone 58 ms later, and back over a third of a second. An update decided before the press was applied after it. It showed only because the press was made dozens of times: three tries had passed. What is shown is now worked out from the state each update is applied to, and the press itself, which stopped the page for 0.4 to 0.7 s, takes no frame longer than 50 ms.

## What a run got wrong

A run can mislead, and one did. The run at the end of Phase 1 found the list of conversations a quarter slower to put away than before, and reported it as the page's. It was the test's own driver: to click a button it finds by its name, it reads the style of some two hundred elements, and that work was timed as the click's. Clicked the way a person clicks, by its place on the screen, the list is put away in one frame. So a probe waits and reads inside the page, clicks by position, and a finding is traced to its cause before it is called one.

## What these numbers are not

- **One machine.** They compare builds on the same laptop. On other hardware the figures differ, and the comparisons should hold.
- **A lab, not the field.** A phone here is a slowed CPU and a slowed connection in a desktop browser, not a phone. Nobody's real visits are measured.
- **Without a model.** The fake model answers at once, at whatever speed the run sets. With a local model, the time to the first word is the model's: seconds on a first question while it loads, a fraction of a second after.
