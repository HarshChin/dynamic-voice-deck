# Engineering Log

A dated record of every meaningful change to the codebase and the reasoning behind it. Where the git history says *what* changed, this log says *why*, what alternatives were considered, and what was learned. One entry per change set; newest at the top of each day.

Entry template:

```
### <YYYY-MM-DD> · <short title> · <commit sha or "uncommitted">
**Scope:** files or modules touched
**Change:** what was done, in two or three sentences
**Why:** the problem or requirement (link PRD feature / TR id / TC id)
**Alternatives considered:** what else was on the table and why it lost
**Verification:** tests added or run, eval impact, measurements
**Follow-ups:** open items created by this change
```

Rules:

- Write the entry in the same commit as the change.
- Record dead ends too. A reverted approach with its reason is more useful than a clean history.
- When a measurement drives a decision (latency, accuracy), put the number here.
- Keep entries factual; opinions go in "Alternatives considered".

---

## 2026-09-11

### 2026-09-11 · A walkthrough you could not interrupt, and why my own fix caused it · uncommitted
**Scope:** `frontend/src/audio/microphone.ts`, `frontend/src/config.ts`, `docs/TRD.md` (TR-116)

**Reported:** "once the walk me through audio starts, and if I interrupt saying hey stop, it just
doesn't and still continues its complete walkthrough."

**It was not the interrupt path.** A WebSocket probe against the running server started a
walkthrough and sent exactly what the browser sends on voice onset: the turn cancelled,
`agent.cancelled` arrived with the right truncation point, and the audio stopped. The server was
never the problem, and neither was the message. The client was not sending it.

**The detector could get into a state where onset was impossible.** Speech detection has two
states: idle, watching for onset, and capturing, watching for the end of a turn. Onset is only ever
declared from the idle state. So any capture that cannot end is a capture that permanently disables
barge-in, and the way a capture ends is 600 ms of silence -- which never arrives while the agent is
talking into the room. A capture opened by a cough, a chair, or a fragment of the agent's own voice
just before the walkthrough started would stay open for the entire deck, absorbing "hey stop" into
an utterance that was never going to be uploaded.

**And my own change that morning made it far more likely.** Conforming to TR-112 dropped onset from
three consecutive loud frames to one while nothing is playing. That is correct, and it is worth
64 ms on every turn -- but it also means a single click or keystroke in the moment before the
walkthrough begins is now enough to open the capture that wedges everything. The earlier rule was
accidentally hiding this. I would rather have the latency and the honest bug.

**Fixed by making a capture always able to end (TR-116).** Two rules. A capture still open at the
moment the agent *starts* speaking is abandoned as a misfire: it did not begin as a question, and
leaving it open is what costs the listener their ability to interrupt. And any capture reaching
twenty seconds ends regardless -- discarded if the agent is audible, because that is its own voice
leaking past echo cancellation and uploading it would ask the model to answer itself, and uploaded
otherwise, because a noisy room is not a stuck detector.

**Why it survived the test suite.** The smoke test did interrupt a walkthrough successfully -- with
push-to-talk, which is key-driven and bypasses the detector entirely. The voice path into a
walkthrough had never been exercised end to end. Eight tests now cover it: six on the detector, and
two at the session seam that drive the exact sequence in order, ending with the interruption that
used to be impossible.

**Verification:** frontend 163 passing, backend 542, five end-to-end cases, `make lint` clean.

### 2026-09-11 · CI caught a flake the machine here never showed · uncommitted
**Scope:** `tests/test_session.py`

**Change:** `test_carry_on_resumes_the_walkthrough_where_it_was_cut` failed on the GitHub runner
with `assert 4 == 3` while passing every time locally. The test waited for the walkthrough to reach
slide 3, sent the interrupt, and then asserted the resume started at 3 -- but the walkthrough keeps
advancing while the interrupt is in flight, so on a faster runner it had already reached slide 4 by
the time the cut landed. The slide is now read after the cancellation rather than before it.

**Worth recording because it is the second time.** The end-to-end resume test made exactly the same
mistake an hour earlier, for exactly the same reason: a number captured before an asynchronous
event is not a fact about what happened after it.

**And the first fix was still wrong.** Reading the slide after the cancellation failed on CI too,
with `assert 3 == 2`, because the cursor advances *before* the slide it advanced to is announced.
An interrupt landing inside that window leaves the server one slide ahead of anything the client
has been told, and no amount of reading the client's messages can close it. The assertion is now a
floor -- at or after the last slide seen, and never slide one -- because that is what the feature
promises. Asserting equality was asserting that the cut cannot land in that window, which is a race
rather than a contract.

The product was never wrong here; the test was, twice, in two different ways.

**Verification:** six consecutive local runs, and CI.

### 2026-09-11 · v0.1.0 · uncommitted
**Scope:** the release

**What shipped.** A voice-first slide presenter that answers spoken questions about a six-slide
deck, navigates by tool call, and can be interrupted mid-sentence. Open-weight models end to end:
Whisper large-v3-turbo and Qwen3.8-27b on Groq, Kokoro-82M in process. Every part of turn-taking
that a speech-to-speech API would have owned -- onset, endpointing, the two tiers of barge-in,
history truncation -- is implemented here and tested.

**The numbers that describe it.**

| | |
|---|---|
| Question asked to first audio | 777 ms, against a 1,500 ms budget |
| Interrupt to `agent.cancelled` | 1.9 ms, with zero frames sent after |
| Audio already in the browser at a cut | 2.6 s -- the reason the client tier exists |
| Walkthrough to first audio | 202 ms, no model call involved |
| Backend tests | 542 passing, zero skipped, 97 % line coverage |
| Frontend tests | 155 passing across 17 files |
| End to end | 5 Playwright cases, green in 11 s |

**Five things worth knowing about how it was built.**

*The tests found the bugs, not the other way round.* Three of the microphone's behaviours disagreed
with their own specification -- the minimum-speech gate counted padding as speech, every upload
carried 600 ms of trailing silence, and onset always waited for three frames -- and none of the
three was visible until the tests for those rows were finally written. Two more came out of the
browser: a checkbox disabled the arrow keys, and the push-to-talk button swallowed the first space
bar press.

*Four failures are recorded that produced no code.* Silero VAD could not be made to load under
Vite, in four distinct ways, each verified in a real browser before the next was tried. The
replacement is an energy threshold, and the deck says so out loud on slide 3.

*The instrument was checked before its readings were believed.* The eval judge is scored against
ten hand-labelled items on every run, half of them deliberately nearly-right, and a run below 90 %
agreement says its own numbers should not be trusted.

*A measurement that measures the wrong thing was fixed twice.* An item refused by the free tier is
excluded from an eval's denominator rather than counted wrong, and the first live barge-in probe
was measuring my own socket buffer rather than the server. Both would have produced a number that
looked fine.

*The free tier shaped the product.* Four questions in two minutes exhausts the per-minute ceiling,
which is why there is a countdown chip rather than silence, why the walkthrough reads its own notes
instead of paraphrasing them through a model, and why the release eval run against the default
model is still pending. That constraint is recorded in `docs/EVALS.md` rather than smoothed over.

**Known and open.** The release eval run for `qwen/qwen3.8-27b` is blocked on the daily budget and
is the first thing to do when it resets. Streaming transcription, a local model path and deck
generation from a topic (PRD F14) are designed and not built.

### 2026-09-11 · Phase 5: evals that can fail, and a README with numbers in it · uncommitted
**Scope:** `backend/evals/` (harness, six suites, four datasets, two judge rubrics, runner,
reporter), `backend/tests/test_evals.py`, `README.md`, `docs/EVALS.md`, `docs/PRD.md`,
`docs/TRD.md`, `app/pipeline/turn.py`

**Change:** The eval suite the TRD has specified since day one, built and wired to `make evals`, and
the README rewritten for release with measured numbers rather than budgets.

**The suites drive the shipped pipeline, not a copy of it.** `run_one` calls the same `run_turn`,
with the same prompt builder, slide controller and keyword fallback; only the socket is replaced by
a list and synthesis is faked for the suites that do not measure it. An eval that reimplemented the
routing logic would pass while the product failed.

**Two measurement decisions that change what the numbers mean.** An item the provider refuses with
a 429 is excluded from the denominator rather than counted as a wrong answer: otherwise a run
during a rate limit measures the free tier, and accuracy moves for reasons unrelated to the code.
And the runner waits out a per-minute limit but not a daily one, because patience does not recover
a spent daily budget; those items are recorded as failures and named in the run's notes.

**The judge is checked before it is believed.** E2 and E3 are graded by the same model family at
`temperature=0` against written rubrics, and the judge is scored on every run against ten
hand-labelled items. Half of those are deliberately *nearly* right, because a judge that only
separates correct from absurd will wave through a vague answer, and vagueness is the failure mode a
presenter actually has. Below 90 % agreement the run says so and its judged numbers are not to be
believed.

**What can be tested about an eval was tested.** Twenty-eight cases cover the deterministic half:
dataset size against what the TRD requires, category coverage, unique ids, every slide an item
names actually existing, the style checker, threshold direction, the summary table's units, and the
judge's tolerance for a model that wraps its JSON in a code fence. The suites themselves are not
tests and never run in CI.

**The smoke test found one thing the screenshot made obvious.** A rate-limited turn was putting
Groq's raw 429 body into the event log panel: a JSON blob naming the organisation id, the billing
URL and the exact token counts. The log is exportable, so that is a small privacy leak as well as
an ugly one. The client now gets a sentence and the wait in seconds; the upstream text stays in the
server log, where it was already.

**Verified in a browser, against the real providers, as one process.** `make serve` builds the
frontend and the backend serves it at `/`. In that build: the deck renders before any session; the
walkthrough speaks real Kokoro audio (786 frames, 3.6 MB); holding the space bar captures a real
utterance (57,344 bytes) that Whisper transcribes word-perfectly as "How do you handle
interruptions?"; the interruption cancels the walkthrough with `truncated_at_sentence_id: 4` and
the log shows "Interrupted after 5 sentences"; the rate-limit chip appears with `retry_after_s: 4`
and counts down; a typed question still gets a spoken answer. No console errors, no page errors.

**One behaviour worth recording as an eval item.** Asked "what are the trade-offs?" while the deck
sat on slide 2, the agent answered from slide 2's notes instead of navigating to slide 6. That is
exactly the kind of miss E1 exists to count, and the utterance has been added to
`routing.jsonl` as `r004`.

**Follow-ups:** the release eval run against the default model is blocked on quota. Qwen's daily
budget was at 198,011 of 200,000 tokens when the suites were finished, and the window releases
roughly 450 tokens a minute, so a forty-item run needs about four hours of waiting. The run against
`gpt-oss-120b` is recorded in `docs/EVALS.md`; the Qwen run is the first thing to do when the budget
resets, and the command is one line.

### 2026-09-11 · Phase 4 closes: a wait you can see, one process, and a browser that proves it · uncommitted
**Scope:** `app/protocol.py`, `app/pipeline/turn.py`, `app/main.py`, `app/session.py`,
`frontend/src/{protocol.ts,store.ts,keyboard.ts,time.ts}`,
`frontend/src/components/RateLimitChip.{tsx,module.css}`, `frontend/e2e/`, `Makefile`

**Change:** The last three items of M4: the rate-limit countdown (TR-171), the single-process build
(TR-212), and the Playwright suite (TC-E2E-001 to 004). Two real defects surfaced while writing the
browser tests, and both were in the product rather than the tests.

**The wait travels as a number, not as a sentence.** Groq answers a 429 with a `retry-after`, and
until now that reached the client only inside an error string. Parsing a duration back out of an
upstream message is exactly the kind of thing that breaks when the provider rewords it, so
`ErrorMsg` gained `retry_after_s` and the chip counts it down. This matters more than it sounds:
four questions in two minutes is enough to hit the free tier's ceiling, and without the chip the
agent simply goes quiet, which reads as broken rather than as busy.

**One process, for someone who just cloned the repository.** `mount_frontend` serves
`frontend/dist` at `/` when it exists, so `make serve` builds the app and runs the whole thing on
one port. It is deliberately **not** part of `create_app`: a mount at `/` matches every path, and
Starlette matches in registration order, so anything a test added to the application afterwards
would be shadowed by it. Keeping it out of the factory is what lets `tests/test_startup.py` keep
adding routes. The mount is not a catch-all either, because the app has no client-side router and
pretending an unknown path exists would be a lie.

**The end-to-end suite runs the real thing against fake providers.** Not a fixed script: the fake
model routes on keywords, so it can answer two different questions differently and a test can
assert an exact slide and exact words, which no real model can promise. Four things had to be got
right before it passed, and each was informative:

- Chromium's fake microphone emits a continuous beep, which the energy detector correctly hears as
  someone talking. The agent interrupted itself the moment it opened its mouth. The device now
  plays the silence fixture; the one test that needs speech drives it by key.
- The fake router matched keywords in the slide-context stamp the prompt prepends, so "explain
  this" asked on the latency slide routed *to* the latency slide. It strips the stamp now.
- The fake's first answer offered "latency, interruption, or tool calling", and the server's
  keyword fallback dutifully moved the deck to a slide the answer had merely mentioned. That is the
  fallback working; the fixture was at fault.
- The fake called `go_to_slide` with an `index` argument rather than `slide_index`, so the call was
  rejected and the fallback moved the deck instead. Worth recording because the log made it
  obvious: the chip said *keyword*, not *model*.

**Two defects the browser found.** Ticking the "debug" checkbox disabled the arrow keys, because
the guard that keeps shortcuts from firing while someone types treated every `<input>` as a text
field. Arrow keys do nothing in a checkbox, so the guard now asks what the focused element would
actually do with the key: `isTypingTarget` for text navigation, `activatesOnSpace` for the space
bar. Separately, clicking "Push to talk" left the button focused, and a focused button is operated
by the space bar, so the first attempt to hold space re-toggled the mode off. The toggle now
releases focus when the click came from a pointer and keeps it when it came from the keyboard,
where the same key has to be able to turn the mode off again.

**"Carry on" resumes the tour.** PRD §13 step 6 asks for it and nothing implemented it: every
trigger phrase started the walkthrough from slide one. Resuming and restarting want opposite things
from the cursor, so they are separate phrases, and the resume only applies while a walkthrough is
what was interrupted. Said in ordinary conversation, "carry on" is a request for more of the
answer, and that belongs to the model.

**Verification:** backend 513 passing, frontend 153 passing across 17 files, five Playwright cases
green in 10 seconds, `make lint` clean. The single-process option was confirmed by hand:
`make serve` answers the app at `/`, its assets under `/assets`, and the API under `/api`.

**Follow-ups:** none for M4. The evals are next, and they are what decides whether the default
model stays Qwen.

### 2026-09-11 · Closing Phase 3: three quiet disagreements between the microphone and its spec · uncommitted
**Scope:** `frontend/src/audio/microphone.ts`, `frontend/src/session/{useSession.ts,usePushToTalk.ts}`,
`frontend/src/keyboard.ts`, `frontend/src/components/{Controls.tsx,Controls.module.css,SlideDeck.tsx}`,
`backend/tests/{fakes.py,test_session.py}`, and five new test files

**Change:** Phase 3 was reported complete while three of its test rows had never been written and two
backend rows were skipped. Writing those tests found three places where the microphone did something
other than what TR-112 and TR-114 say, none of which any existing test could see. All three are fixed,
push-to-talk is implemented, and the two skipped backend rows now run.

**The minimum-speech gate was measuring the wrong thing.** An utterance is assembled as padding plus
speech, and the gate that rejects a cough subtracted `this.#preRoll.length` from the frame count. By
that point the pre-roll has been moved into the utterance and the field is empty, so the subtraction
was always zero and the gate counted 300 ms of padding as speech. A 160 ms cough cleared a 250 ms
minimum and was uploaded. The fix records how many padding frames were actually prepended, which is
not recoverable afterwards: an onset in the first third of a second has less padding than a full
window. Pinned by `TC-FE-022`, which fails against the old arithmetic.

**Every upload carried 600 ms of silence the recogniser did not need.** TR-114 says the utterance is
`prePad + speech`. The endpointer ends a turn on a run of silent frames and was shipping that run with
the audio, so a two-second question uploaded as 2.6 seconds and the transcriber waited for all of it.
Trimming is safe by construction: every frame in that run is below the silence threshold, so none of
them carries a word.

**Onset always demanded three consecutive loud frames.** TR-112 asks for three only while the agent is
audible, where the reason is echo leaking through cancellation, and one otherwise. The detector had no
way to know, so it was cautious always and charged 64 ms of latency for it on every turn that
interrupts nothing. The session already owns the answer, so it passes `isPlaying` in. A false onset
while nothing is playing is cheap: it emits `speech.start` and the minimum-speech gate still refuses to
upload the noise that caused it.

**Push-to-talk (TR-115) is the escape hatch energy detection needs.** The detector has one failure a
person cannot work around: a room loud enough that every frame reads as speech, where the agent is
interrupted by the room. A held key is immune to that, and some people simply prefer explicit turns.
The key path reuses the whole utterance assembly, so padding, the minimum-speech gate and barge-in
behave identically whichever thing opened the turn. The binding stands aside for typing, for modified
keys and for auto-repeat, and it treats losing the window as a release, because a key held while the
user switches applications never reports going up and would otherwise leave the microphone open for
ever.

**Two backend rows stopped being skipped.** `TC-BE-056` (an oversized utterance closes the socket with
1009) and `TC-BE-060` (a sentence whose synthesis fails is skipped and the rest still speak) were both
skipped with a milestone that has since shipped. Implementing them needed a failure hook in `FakeTTS`
and one honest fix in the WebSocket harness: the raw transport reports a server-side close as an
ordinary frame, so the harness was silently treating a closed socket as a malformed message. It now
raises `WebSocketDisconnect` the way a browser would see it, which is what makes a close assertable at
all. The backend suite now has **zero skipped tests**.

**Verification:**

| Measurement | Value |
|---|---|
| Interrupt to `agent.cancelled`, live | 1.9 ms |
| Audio frames sent after the cancellation | 0 |
| Audio already in the browser at the moment of the cut | 2.6 s |
| First audio after asking, `qwen/qwen3.8-27b` | 777 ms |
| First audio after asking, `openai/gpt-oss-120b` | 2,066 ms |

Backend 503 passing, zero skipped, 96 % coverage. Frontend 133 passing across 15 files. Push-to-talk
verified in a real browser against the running backend: twelve checks including that a 1.7 second hold
uploads 61,440 bytes, that the space bar does not scroll the page, and that turning the mode off
releases the binding, with no console or page errors.

**The number worth keeping is 2.6 seconds.** That is how much audio the browser was already holding
when the interruption arrived, because the server streams synthesis faster than the room can hear it.
It is the entire argument for the client tier: the server stopping in 1.9 ms is not what makes the
agent go quiet, and a design that only cancelled server-side would have kept talking for another two
and a half seconds.

**A near miss worth recording.** The first live probe decoded sentence ids as 16,777,216. The wire
header is little-endian on both sides and the probe read it big-endian, which is a bug in the probe.
The uncomfortable part is that the new browser test had made the same mistake and passed anyway,
because sentence 0 is identical in either byte order and the assertions only needed ids to be
distinct and ordered. Both are now little-endian and say so.

**On the model.** The backend had been left running with a `GROQ_LLM_MODEL=openai/gpt-oss-120b`
override from an earlier testing session while `.env` said `qwen/qwen3.8-27b`. It has been restarted
from `.env`. Under gpt-oss one turn in three died with `Failed to parse tool call arguments as JSON`
from upstream and time-to-first-token was 1,530 ms; under Qwen the same question answers in 777 ms.
Both belong to the model, not the pipeline, and no prompt was changed for either.

**Four more things this closing pass turned up.**

*The audio path had no test at all.* Every session test asked by typing, because for most of the
state machine the two paths are the same code. They are not the same in ``handle_utterance``, which
owns the size guard, the transcription call, and the two ways a transcript can be worth nothing.
That branch had never been executed by a test. It is now, including that the session stays in
HEARING while the transcriber works: announcing THINKING there would carry the previous turn's id,
so a client would see two THINKING transitions with different ids for one question. Three rows that
had been marked *partial* because "no session test uploads an utterance" are now simply passing.

*A green run was popping a crash dialog.* The suite reported success and then aborted at interpreter
shutdown with a mutex error from ONNX Runtime's destructor, which on macOS means a "Python quit
unexpectedly" report for a passing build. The test that loads the real Kokoro weights is now marked
``integration`` alongside the ones that spend an API key, which is the honest classification anyway:
it loads a 300 MB model, needs weights the repository does not carry, and is not a unit test. It
runs in isolation without aborting, so ``make test-integration`` is clean too.

*The error mapping keyed off string literals.* ``provider_error_message`` matched provider names as
strings and defaulted to ``llm_failed``, so renaming the transcriber would have silently reported
every transcription failure as a model failure. It now keys off the providers' own name constants.

*Integration tests exist now, and four of the five have been run.* ``TC-INT-001`` and ``002``
transcribe real audio through Groq; ``003`` proves the model, not a keyword, routes a question to
slide 4; ``005`` measures first audio through the whole pipeline at 777 ms against a 1,500 ms
budget. The utterances are synthesised by the project's own Kokoro voice and resampled to 16 kHz by
the same naive interpolation the capture worklet uses, so a fixture is exactly what the browser
would have uploaded; a better resampler would give the recogniser an easier file than the product
can actually send. ``TC-INT-004`` is written but unrun: the free tier's daily token budget for the
configured model was spent.

**Follow-ups:** the free tier's per-minute input ceiling is reachable in normal use, and four turns in
two minutes hit it. A turn that is rate-limited after it has already spoken stops mid-answer with an
error in the log. That is TR-171's rate-limit chip, still open in Phase 4.

### 2026-09-11 · A walkthrough that reads its own notes, and never calls the model · uncommitted
**Scope:** `app/pipeline/turn.py`, `app/session.py`, `frontend/src/session/useSession.ts`,
`frontend/src/components/Controls.{tsx,module.css}`, and their tests
**Change:** "Walk me through it" now presents the whole deck. The model is not involved: the
walkthrough speaks each slide's speaker notes directly.

**That is the design, not a shortcut.** Notes are already written to be spoken; that is what they are
for. Asking a model to paraphrase them would cost roughly 21,000 input tokens against a free-tier
ceiling of 7,000 a minute, which is three minutes of the agent standing silent between slides, and it
would add the one step that could drift from the source it is otherwise told to stay faithful to.
Reading them costs nothing and starts immediately: **first audio 202 ms**, against about 800 ms for a
model turn.

**It is still interruptible, which is the point.** Each slide's sentences go through the same sender
as an answer, so speech onset cuts the walkthrough off exactly as it cuts off a reply, and history is
truncated to what was actually heard. The question that follows is then answered by the model in the
ordinary way, with the deck already on the slide it was interrupted on.

**The trigger phrase is matched in code, not by the model.** Slide 1 tells the listener to say
exactly those words, so it has to work every time; a model round trip to reach a conclusion we
already have costs a fifth of a free-tier minute; and the feature it starts involves no model at all,
so that would have been the only model call in it. Matched on the shared turn path, so typing and
saying it do the same thing.

**Verified:** all six slides in order, 101 segments, 308 seconds of speech, zero model calls. Buttons
verified in a real browser, including that mute reports its state rather than being a toggle the user
has to remember.

### 2026-09-11 · Phase 4 opens with two defects the first voice session exposed · uncommitted
**Scope:** `app/session.py`, `app/pipeline/turn.py`, `tests/test_session.py`
**Change:** The owner's first real spoken session worked -- transcription was word-perfect on every
question -- and surfaced two problems that only voice input can produce.

**The agent interrupted itself.** Twice, a turn was cancelled a fraction of a second after starting,
showing an interrupt chip for something nobody interrupted. The cause was the tail of the user's own
sentence arriving as a fresh speech onset, and onset while THINKING was treated as barge-in. It
should not be: nothing has been said, so there is nothing to cut short, and cancelling throws away
work the user is still waiting for. Onset now only interrupts while SPEAKING. Nothing is lost by
waiting -- if the onset really is a new question, its utterance supersedes the running turn a moment
later, which is the same cancellation taken where it is known to be wanted.

**That exposed a worse problem.** The session only entered SPEAKING *after* a turn finished, so it
reported THINKING for the entire time it was talking. Keying barge-in on SPEAKING would therefore
have disabled it completely. SPEAKING is now announced as the first audio frame leaves, which is both
honest and what makes an interrupt meaningful.

**Which in turn exposed a third.** That announcement comes from the speech sender's own task, and the
sender can be parked in a send when the turn is cancelled -- deliberately, since cancelling mid-write
corrupted the socket in Phase 2. Waking up afterwards it would have stamped the *next* turn's id on a
transition belonging to the abandoned one. The callback now carries its turn id and checks it. The
test that guarded the old behaviour asserted "cancelled inside the send", which is no longer the
guarantee; it now asserts the one that replaced it, that each transition carries the id of the turn
that produced it.

**Also from the same log, and left alone deliberately:** `gpt-oss-120b` answered three questions with
"Sorry, I lost that one", our own fallback for a turn that produces nothing. The server log shows
`finish_reason=length` with `chars=0`: a reasoning model spending the whole 350-token response budget
on reasoning tokens, which are charged against it but never appear in the content. That is a
model-specific failure. Qwen, the configured default, does not do it, and the owner asked that the
prompt and token budget not be changed to accommodate a model we are not shipping. Recorded here
because the same symptom will reappear behind any reasoning model.

**Still pending:** the backend is running on `openai/gpt-oss-120b` because Qwen's daily budget was
spent. Switch it back.

### 2026-09-11 · Silero replaced by an energy detector, after four verified failures · uncommitted
**Scope:** `frontend/src/audio/microphone.ts`, `frontend/public/worklets/capture.js`,
`frontend/package.json`, `frontend/vite.config.ts`, `app/decks/anatomy_of_a_voice_agent.json`,
`docs/TRD.md` (TR-110, §2.2, §3.2)
**Change:** Speech detection no longer uses Silero VAD through `@ricky0123/vad-web`. It is an audio
worklet in this repository measuring per-frame loudness, with hysteresis and the same timings the
design specified.

**Why, in the order the failures arrived, each reproduced in a real browser:**
1. Copying the ONNX runtime's WebAssembly loader into `public/` and pointing the runtime at it:
   Vite refuses to let source import a module from `public/`, by design and by name in the error.
2. Removing it and letting the runtime resolve its own loader: Vite's dependency pre-bundler rewrites
   that dynamic import into `.vite/deps/`, where the loader was never copied. "Failed to fetch
   dynamically imported module."
3. Excluding the runtime from pre-bundling: fixes that and breaks the detector instead, because
   `@ricky0123/vad-web` is CommonJS and *depends* on pre-bundling to be importable by name. Blank
   page, "does not provide an export named 'MicVAD'".
4. Setting `wasmPaths` on the runtime: silently ineffective, because the detector imports
   `onnxruntime-web/wasm` while I was configuring `onnxruntime-web` -- two entry points, two module
   instances, two `env` objects. Reading the bundled source then showed that naming the loader path
   is precisely what forces the fetch, so the correct move was to say nothing about it. Removing it
   returned to failure 2.

**The turning point was tooling, not insight.** Three of those attempts were shipped to the owner and
failed in front of them, because a terminal cannot execute a browser's module graph. Installing
Playwright and loading the page headlessly turned a guess-and-ask loop into a five-minute one, and
found failure 4 immediately. That should have happened after the first screenshot.

**What replaced it.** An `AudioWorkletProcessor` downmixes, resamples to 16 kHz, and reports the RMS
of each 32 ms frame; the main thread applies hysteresis, pre-roll, and a minimum speech duration.
Verified headlessly with a fake capture device: the session moved `listening -> hearing` on the
device's tone, with no page errors.

**The trade-off, stated plainly and put on the slide.** A neural detector distinguishes speech from
other sounds far better, especially in a noisy room. An energy threshold is adequate for the question
this product asks -- has the person started, have they stopped -- particularly with echo cancellation
suppressing the agent's own voice. It also removes a WebAssembly runtime, a model download, 100 MB of
vendored assets and a CDN dependency, which for a demo that has to work on a strange network is worth
something by itself. Slide 3's notes and TR-110 now say all of this; the deck describes its own
architecture, so leaving it claiming Silero would have been a lie the agent tells out loud.

### 2026-09-11 · Phase 3: the microphone, and interruption that is actually felt · uncommitted
**Scope:** `app/providers/groq_stt.py`, `app/providers/registry.py`, `app/session.py`, `app/main.py`,
`app/pipeline/{turn,metrics}.py`, `frontend/src/audio/microphone.ts`,
`frontend/src/session/useSession.ts`, `frontend/scripts/copy-vad-assets.mjs`, and their tests
**Change:** Speech now goes in as well as out. Whisper on Groq transcribes one finished utterance per
turn, Silero voice detection runs on-device in the browser, and speech onset while the agent is
talking silences it before the server hears about it.

**Measured against the real services**, question spoken by Kokoro, downsampled to 16 kHz exactly as
the browser will send it: transcription **265 ms** and word-perfect, model first token 483 ms,
synthesis first byte 284 ms, and **1,048 ms from utterance to first sound**.

**The ordering is the design.** On speech onset the browser flushes playback first and tells the
server second. A round trip in that order would be audible; in this order the sound is gone before
the interrupt message has left. The server's job is the slower half: cancelling the turn and cutting
its memory back to the sentences that were actually heard.

**Assets are served from `public/vad/`, not a CDN.** The detector fetches its model and worklet at
run time and defaults to a third-party host. A microphone feature that silently depends on someone
else's uptime is a demo waiting to fail on a conference network, so `npm run postinstall` copies them
locally, where they are pinned by the lockfile and git-ignored.

**A bug fixed by giving up on persuasion.** The second request of a navigating turn kept reopening
with the sentence the first had already spoken -- "Two layers, actually. Two layers, actually."
Merging the spoken text into the tool-call message reduced it; instructing the model not to repeat
reduced it further; neither removed it. It is now enforced in code: a sentence already spoken this
turn is not spoken again. Dropping a genuine repeat costs a listener nothing, and hearing the same
line twice is the kind of flaw that makes a demo feel broken. Third time this phase that a
deterministic guarantee beat an instruction to the model.

**Transcription is refused below 100 ms of audio.** Whisper answers a click with a confident
hallucination rather than with silence, so the shortest uploads are rejected before they cost
anything.

### 2026-09-11 · Slide context moved inside the question, after two weaker attempts failed · uncommitted
**Scope:** `app/pipeline/prompt.py`, `app/prompts/presenter.md`, `tests/test_turn.py`
**Change:** Asked "what's this slide about" the agent kept answering about whichever slide it last
spoke about, not the one on screen. This is the third attempt at the same defect, and the first two
are worth recording because they looked sufficient and were not.

1. **Naming the slide in the system prompt.** Lost to a near-identical exchange further down the
   conversation: asked the same question twice on different slides, the model replayed its first
   answer word for word.
2. **A system message immediately before the question.** No better. The question was identical to
   the earlier one and the earlier answer sat directly above it.
3. **A system message appended after the question.** Passed once in testing, then failed again in
   the owner's hands, which is the more honest sample.
4. **What works: stamping the context into the question itself**, so each turn reads
   `[Looking at slide 6 of 6: "Trade-offs and What's Next"] What's this slide about`. A model cannot
   skim past a phrase inside the sentence it is answering. The prompt explains that a bracketed
   prefix is context, is never read aloud, and is the truth about where the deck is now even when an
   earlier answer was about somewhere else.

**Verified** across three hand-navigations in one session with history accumulating: slide 4 answered
about interruption, slide 6 about trade-offs, and slide 1 about the system, with no wrong navigation
and no bracket spoken.

**Lesson about verification.** Attempt 3 was tested once, passed, and shipped. The owner found it
still broken within minutes. One live pass is not evidence for a non-deterministic failure; the test
now walks several slides in one session so the history that causes the bug is actually present.

**Also changed:** the development server now runs with `--reload`. It had been started without it, so
a fix could be committed while the running process still served the old code. Removing that class of
confusion is worth more than the reload cost.

### 2026-09-11 · A wrong answer dragged the deck off the slide the user chose · uncommitted
**Scope:** `app/pipeline/prompt.py`, `app/pipeline/slides.py`, `app/session.py`, and their tests
**Change:** From an exported session: on slide 6 the owner asked "What's this slide about" and got an
answer about slide 1, after which the deck jumped to slide 1. Two bugs compounding.

**The model reused its previous answer.** The same question had been asked two turns earlier while
slide 1 was on screen, and the reply came back word for word. The just-in-time reminder naming the
current slide was already in place but sat *before* the user's question, where it lost to an
identical exchange sitting immediately above. Moved it to the very end, after the question, so it is
the last thing the model reads, and sharpened it to say the deck may have moved and that an earlier
answer must not be reused. Recency is what decides this; the third position finally worked.

**Then the keyword fallback made it worse.** The wrong answer scored against slide 1's aliases, so
the fallback moved the deck to slide 1 to match — overriding five deliberate arrow-key presses.
A hand-driven move is an explicit statement of what the user wants to look at, and a scorer reading
the *answer's* wording has no business overruling it. The fallback is now suppressed for the turn
following any manual navigation, and resumes on the next turn.

**Verified** on the exact sequence: answer on slide 1, arrow to slide 6, ask the identical question,
and it answers about trade-offs without touching the deck.

**Note on the rate limit:** the exported log shows the free tier is 7,000 *input* tokens per minute,
not 8,000 total as the earlier error implied. With a prompt near 1,800 tokens and up to three
requests per navigating turn, a question every 30 seconds is about the sustainable rate.

### 2026-09-11 · The agent moved the deck and said almost nothing · uncommitted
**Scope:** `app/prompts/presenter.md`, `app/pipeline/turn.py`, `tests/test_turn.py`
**Change:** The owner exported a session in which every answer was one short sentence, one turn said
only "Here's slide 3.", and one repeated the previous answer verbatim. Three causes, all in how the
two-request turn is driven.

**The opener was being treated as the whole answer.** The prompt asks for a short opening sentence
because the first segment is synthesised before the rest is generated, which is worth about a second
and a half of perceived latency. The model followed it and stopped: 21 characters, then done. The
server metrics made it unmistakable, with only three to nine milliseconds between first token and
finish. The prompt now says outright that the opener is a way into the answer and never the answer,
and that a single sentence is not a reply.

**The second request kept calling tools instead of speaking.** Asked "what is considered a misfire?",
the model called `go_to_slide`, then on the next request called `highlight_bullet`, both with empty
content, and the turn ended having moved the deck in silence. A system note telling it not to call
another tool did not stop it. Raised the ceiling from two requests to three, so a turn always gets a
chance to speak, and the loop still exits the moment any words appear, so ordinary turns never pay
for it. The cost is real on a free tier at 8,000 tokens a minute, but a slide changing in silence is
the worse outcome.

**The opening line was said twice** when the model spoke before navigating. The continuation note now
quotes what has already been said aloud and asks the model to carry on from exactly there, rather
than asking for an answer it has partly given.

**Verified** on the owner's exact questions: "what is considered a misfire?" now answers in seven
segments on the right slide, and "what are the latency budgets" in nine, with no repetition.

**Worth recording about method:** the log the owner exported was more useful than any test, because
it contained the model's real output across four turns of accumulated history. Every one of these
failures needs a real model and a real conversation to appear.

### 2026-09-11 · Phase 2: the agent speaks · uncommitted
**Scope:** `app/providers/kokoro_tts.py`, `app/pipeline/turn.py`, `app/session.py`, `app/main.py`,
`app/providers/registry.py`, `app/config.py`, `frontend/src/audio/playback.ts`,
`frontend/src/session/useSession.ts`, `frontend/src/components/{Orb,Controls}.tsx`, and their tests
**Change:** Kokoro-82M synthesises on this machine, the server streams 100 ms PCM16 frames over the
same socket as the JSON, and the browser schedules them gaplessly on the Web Audio clock. 474 backend
and 83 frontend tests pass.

**Measured before designing, which changed the design twice.**
- Kokoro's `create_stream` exists, which I had not known when writing TR-083. It yields one chunk for
  sentence-sized input, so it buys nothing here, but it does stop work when abandoned. Kept `create`
  in a thread for simplicity and documented why.
- Synthesis costs about **8 ms per character**: a 21-character opener is audible in 259 ms, an
  ordinary sentence in 440 ms, three sentences in 1,677 ms. This is the measurement that justifies
  the whole early-split design. The prompt's "open with a short sentence" instruction, written on a
  hunch in Phase 1, is worth roughly a second and a half of perceived latency.
- Sample rate confirmed at 24,000 Hz, matching what the client was already built for.

**End to end, measured:** first audio **760 ms** after the question against a 1.5 s budget, with the
model's first token at 480 ms and synthesis first byte at 272 ms.

**Four bugs that only running it could find:**
- **Cancelling the speech task mid-send corrupted the WebSocket**, killing the connection rather than
  the turn. It now sets a flag and lets the sender stop at a frame boundary, which is all barge-in
  needs: no further audio, not a thread stopped mid-write. Awaiting cleanup inside a `finally` on a
  coroutine that is itself being cancelled is how cleanup gets interrupted half-done, so `stop()` is
  deliberately synchronous.
- **A dead sender deadlocked the queue for ever**, because nothing calls `task_done` after the
  consumer dies. `submit` and `drain` now re-raise whatever killed it. This defect found itself:
  the guard turned a hang into a clear `AttributeError` from an unrelated ordering mistake.
- **The agent said "Two layers, actually. Two layers, actually."** Replaying already-spoken text as a
  trailing assistant message leaves the conversation ending on the assistant's own turn, and a model
  asked to continue from there starts its reply again. Merging it into the assistant message that
  carried the tool call -- the documented shape -- leaves the tool result last, which reads as a
  request to continue. The repetition stopped.
- **A flag was read before it was assigned**, because `create_task` can schedule the coroutine before
  the constructor finishes.

**A design decision worth recording:** an interrupt and an error now do opposite things with queued
speech. A barge-in drops it, because the user is already talking. A provider failure part-way through
drains it, because a partial answer beats silence followed by an error. That distinction lives in the
sender's `__aexit__`, which is why it is a context manager.

**Client-side playback.** Frames are scheduled on `AudioContext.currentTime` rather than played on
arrival, because that clock advances smoothly where `setTimeout` does not. A late frame is delayed
rather than overlapped: a short silence is far less noticeable than two frames playing at once. The
queue also owns two things nothing else can know -- when a sentence actually reached the speakers,
which the server needs to truncate its memory honestly, and the flush that makes tier one of barge-in
instant without a round trip.

**I walked into my own IPv6 trap** while writing the capture script, pointing it at `localhost:8000`
and reaching the unrelated PHP server documented in the Phase 0 entry.

**Follow-ups:**
- Time to first token is 480 ms against a 250 ms budget. Two model round trips per navigating turn is
  the structural cause.
- The orb now scales with measured output level while speaking. Microphone-driven levels arrive with
  capture in M3.

### 2026-09-11 · Two bugs found by using the app, not by testing it · uncommitted
**Scope:** `app/pipeline/{prompt,slides}.py`, `app/prompts/presenter.md`, `frontend/src/store.ts`,
`frontend/src/components/EventLog.tsx`, and their tests
**Change:** The owner drove the app and exported a session log. Two defects fell out that the suite
could not have caught, because both are about how a real model and a real reader behave.

**Bug 1: the agent answered about the wrong slide.** On slide 4, asked "what's this slide about",
it replied "This is the intro slide." The server log proved the plumbing was right: the manual
navigation arrived, `current_slide` was 4, and slide 4's notes were in the prompt. The model simply
failed to connect the bare number in `current_slide: 4` to the deck entry. A second export showed a
harder version: asked the same question on slide 5 and again after moving to slide 4 by hand, the
model replayed its slide-5 answer word for word.
Two fixes. The position block now names the slide rather than numbering it, and states that "this
slide" means that one and nothing else. And a short system message naming the slide on screen is
inserted immediately before the user's question, because recency beats a position block hundreds of
lines earlier when a near-identical exchange is sitting right above the new question. Verified
against the real model on the exact sequence from the export.

**Bug 2: "tell me in one line" produced three chat bubbles.** The model complied and wrote one
sentence; the chunker split it into three speakable segments at clause boundaries, and the event log
rendered one bubble per segment. Segments exist so synthesis can start on the first clause instead
of waiting for the whole answer (TR-042), which is a latency device, not a message boundary. The
store now merges consecutive segments of a turn into one entry that keeps the segments inside it, so
the log shows one answer while barge-in can still strike through exactly the segments nobody heard.

**Why this is worth recording:** 475 backend and 73 frontend tests were green through both defects.
Neither is reachable by a test, because one depends on how a specific model resolves an ambiguous
reference and the other on what a person reading the screen concludes. Driving the product remains
the only way to find this class of bug.

### 2026-09-11 · Phase 1 review resolved: 35 findings, every fix mutation-tested · uncommitted
**Scope:** `app/session.py`, `app/pipeline/{history,turn,slides,chunker}.py`,
`app/providers/{groq_llm,base,registry}.py`, `app/decks/`, `app/prompts/presenter.md`,
`app/protocol.py`, `frontend/src/`, `docs/TEST_CASES.md`, and their tests
**Change:** Resolved all 35 findings from the five-lens Phase 1 review, 14 of them high severity.
Six agents worked disjoint areas in parallel; each reverted its own fix individually and re-ran the
suite to confirm the new test actually failed against the old behaviour. Test count rose from 402 to
475 backend and 61 to 73 frontend, at 99% coverage.

**The findings that mattered most were all in the barge-in foundation**, which is harmless in a
text-only milestone and fatal the moment audio lands:
- **History could never be truncated once the model finished generating.** `add_assistant` cleared
  the pending turn, so `truncate_current` silently returned False and every unheard sentence stayed
  in history as though it had been spoken. With audio this becomes the common case, because the
  model finishes generating long before playback finishes. The pending turn now stays addressable
  until the next turn begins, and the session acts on the returned boolean instead of reporting
  success regardless.
- **Three abnormal exits discarded what the user had already heard.** The watchdog, the provider
  error path, and pause all cancelled without truncating, so a rate limit mid-answer left history
  claiming the agent never spoke. All abnormal ends now funnel through one helper that cuts history
  to the last sentence actually sent.
- **An unexpected exception wedged the session in `thinking`** with no error frame and nothing logged,
  because only three exception types were caught. Real sources existed already.
- **The interrupt debounce was keyed on wall-clock time alone**, so a legitimate barge-in on a newly
  started turn within 500 ms of the previous one was dropped and the agent talked over the user for
  the rest of that turn. The window is now scoped to the turn being interrupted.

**Two tests were proved hollow.** A reviewer showed the playback handler could be replaced with a
bare `return`, and the cancellation call enforcing the single-turn rule (TR-022) could be deleted
outright, with all 402 tests still passing. Both mutations are now killed.

**The deck was asserting falsehoods about the system it demonstrates.** Slide 5 described an
architecture that stopped being true when the prompt changed earlier in the phase. For a deck whose
subject is its own architecture, that is a product defect, not a documentation nit.

**Cross-cutting fixes I made directly**, because they span both languages: `slide.goto` now carries
`turn_id`, so the one message that changes what the audience sees is no longer the only one exempt
from the stale-turn guard; and `internal_error` is now a distinct error code, so an unexpected
failure no longer reports itself as a model failure.

**Catalogue reconciliation.** Parallel authoring produced 20 duplicate TC ids across test files.
Renumbered `test_history.py` into the 220 block and `test_turn.py` into 232-237, then rebuilt the
catalogue's ids from the code rather than by hand, since the code is the authority. The document is
now 293 rows with no duplicate id on either side and one uncatalogued reference, which is a section
comment rather than a claim.

**Interruption during a run cost roughly an evening.** The organisation's monthly spend limit killed
all six agents mid-flight. They had written nothing, leaving only a self-labelled temporary probe
file that broke lint. Recovery was to remove it, commit the working checkpoint so a second
interruption could not cost the work, and relaunch from the saved workflow script. Worth recording
as a process lesson: commit a green checkpoint before a long parallel run, not after.

**Verification:** `make lint` and `make test` clean. Against the real API after the fixes, all three
smoke questions behave: navigation to slide 5 and 4 with the turn id now stamped on the navigation,
and an off-topic question declined without moving the deck.

**Follow-ups:**
- Time to first token measured 0.9-1.2 s, better than the 3.5 s seen before the prompt was cut, but
  still far outside the 250 ms budget in TRD §8.1. Two model round trips per navigating turn is the
  structural cause. Revisit before audio sits behind it.
- The abnormal-exit path writes `[interrupted by user]` even when the cause was a timeout or a
  provider failure. It produces the right model behaviour but is slightly untrue; a distinct marker
  is worth considering in M2.
- TRD TR-023 sets the interrupt refinement window at 200 ms while the implementation reuses the
  500 ms debounce constant. One window instead of two; reconcile the document.

## 2026-09-10

### 2026-09-10 · Phase 1: two bugs the first end-to-end run exposed · uncommitted
**Scope:** `app/pipeline/turn.py`, `app/pipeline/prompt.py`, `app/prompts/presenter.md`,
`app/session.py`, `app/main.py`, `.env`, `.env.example`, several tests
**Change:** Built the text loop -- deck repository, protocol, history, slide controller, chunker,
prompt builder, Groq streaming client, session state machine, turn pipeline, and the WebSocket and
deck routes -- then drove it against the real Groq API and fixed what that revealed.

**Bug 1: the agent moved the deck and said nothing.** The first run answered "how do you handle it
when I interrupt you?" by navigating correctly to slide 4 and then producing zero words. The model
returned `finish_reason=tool_calls` with empty content, which is simply how tool calling works: the
model emits the call and stops, and the caller must send the tool results back in a SECOND request
to get the spoken answer. `run_turn` made one request. Added a two-step loop with a hard ceiling of
two round trips. The follow-up call deliberately offers no tools, which both stops the model
navigating twice for one question and saves the tool schemas' ~320 input tokens.

**Bug 2: the free tier could not afford the prompt.** The third question came back HTTP 429. Groq's
free tier allows 8,000 tokens per minute for every model offered (checked across gpt-oss-120b,
gpt-oss-20b and qwen3.8-27b via the rate-limit response headers, so switching model does not help).
The prompt was 3,262 input tokens, capping the agent at two requests a minute -- less than one
exchange, since a turn that calls a tool needs two. Cut to 1,815 tokens, a 44% reduction, by:
- **Sending notes only for the slide on screen.** Other slides contribute title and bullets, which
  is all the model needs to decide where to go. To speak about another slide it must navigate first,
  which is the behaviour the prompt already asked for. Worth ~1,570 tokens.
- **Not sending aliases at all.** They exist for the server-side keyword fallback in
  `SlideController`, which runs in Python after the model answers. The model never needed them.
  Worth ~300 tokens.
- **Tightening the system prompt** from 5,548 to 3,730 characters with every behavioural rule intact.

**Why this matters beyond the free tier:** input tokens are also latency. The measured
time-to-first-token was 3.5 s on the two-step path, which is well outside the 250 ms budget in TRD
§8.1 and will need attention in M2 when it sits in front of speech.

**Verification:** After the fixes, "how do you handle it when I interrupt you?" navigates to slide 4
and answers in seven segments with content drawn from the notes, opening "Two layers, actually." --
the short opener the prompt asks for, which exists because the first sentence is synthesised before
the rest is generated. "What's the weather in London?" correctly navigates nowhere and gives a
one-sentence redirect. 279 backend tests and 27 frontend tests pass; ruff, mypy --strict, eslint,
tsc and prettier are clean.

**Other decisions:**
- **Provider defaults now match the milestone.** The registry fails fast on providers whose
  milestone has not landed, so `.env` and `.env.example` select `fake` for speech-to-text and
  text-to-speech, with a comment saying when each flips to the real thing. Without this a fresh
  clone would not start.
- **Tests that boot the app now request a `fake_providers` fixture** rather than inheriting ambient
  defaults, and two assertions that hard-coded provider names were rewritten to compare against the
  configuration, which is what the health probe actually promises.
- **Helper functions in `turn.py` are keyword-only.** They take six same-typed arguments and a
  positional mix-up would be silent.

**Follow-ups:**
- Time to first token is 3.5 s against a 250 ms budget. Investigate before M2 puts audio behind it.
- The model's answer used typographic quotes, which a speech synthesiser may voice oddly. Normalise
  in the chunker during M2.
- Sentence count is measured in speakable segments, not grammatical sentences, so a four-sentence
  answer reports as seven. Make sure the metric's name does not mislead in the HUD.

### 2026-09-10 · Process change: drop the adversarial verify stage from reviews · uncommitted
**Scope:** working process, no code
**Change:** The per-phase loop becomes **code -> review -> resolve -> smoke test**. The separate
skeptic pass that independently tried to refute every review finding is removed.
**Why:** Owner's call. On Phase 0 the verify stage cost 39 extra agents to refute 20 findings, and
its main value (catching overstated severity) is cheaply replaced by the resolver simply reading the
code before acting. The two refuted findings that mattered were ones I overrode anyway, which is the
judgement the loop needs, not another vote.
**Trade-off accepted:** Some findings will now be actioned that a skeptic would have shown to be
wrong, so review output must be read critically rather than applied mechanically. Reviews stay
multi-lens and parallel; only the second stage goes.

### 2026-09-10 · Phase 0 shipped; a self-inflicted CI failure worth recording · 191b134, 4b0df8e, 56529bc
**Scope:** `.github/workflows/ci.yml`
**Change:** Pushed Phase 0 and confirmed CI green on a clean ubuntu-24.04 runner in 26 s. Then bumped
the three actions off Node 20, broke the build, and fixed it.
**Why:** GitHub annotates every run with a Node 20 deprecation notice for `actions/checkout@v4`,
`actions/setup-node@v4`, and `astral-sh/setup-uv@v6`. Clearing it now removes a deadline from the
critical path later.
**The mistake:** I read the version from each repository's *latest release* (`setup-uv` reports
`v10.0.1`) and assumed a matching moving major tag `v10` existed. It does not — `setup-uv` publishes
releases well ahead of its major aliases, whose newest is `v7`. CI failed with
`Unable to resolve action astral-sh/setup-uv@v10, unable to find version v10`.
**Correction:** Query the git refs API for tags matching `^v[0-9]+$` and pin to the newest alias that
actually exists, then verify every `uses:` line resolves *before* pushing rather than letting the
runner find out. Now on `checkout@v7`, `setup-uv@v7`, `setup-node@v7`.
**Lesson worth keeping:** a release name is not a tag. For anything referenced by tag — actions,
container images, git submodules — resolve the exact ref before committing to it. The
pre-push verification loop is three lines and would have caught this.
**Verification:** Run 34488220964 green in 35 s, no annotations. History left honest rather than
force-pushed over: the red commit and its fix both stand.

### 2026-09-10 · Phase 0: scaffolding, quality gates, and the review that paid for itself · uncommitted
**Scope:** `backend/` (pyproject, app/{config,errors,logging_setup,main}.py, tests/), `frontend/`
(Vite app, ESLint/Prettier/Vitest config, placeholder UI), `Makefile`, `.github/workflows/ci.yml`,
`.editorconfig`, `.gitignore`, `.env.example`, `README.md`, `docs/{PRD,TRD,TEST_CASES}.md`, `CLAUDE.md`
**Change:** Stood up the repository: uv-managed Python 3.12 backend with settings, an error hierarchy,
structlog with a standard-library bridge, and a health endpoint; a Vite/React frontend with a
placeholder page that probes the backend; Make targets, CI, and the first 112 tests. No product
behaviour yet, by design.
**Why:** Phase 0's job is to make every later phase cheap. A wrong contract in settings, logging, or
the async lint rules would propagate into the pipeline code where it is expensive to unpick.

**Alternatives considered:**
- *Keep the Vite template's `oxlint`* — rejected in favour of `typescript-eslint` with type-checked
  rules. `no-floating-promises`, `no-misused-promises`, and `await-thenable` guard the audio teardown
  and WebSocket code that this project lives or dies on, and `oxlint` has no type-aware equivalent.
  Cost: a slower lint. Worth it.
- *Install the backend as a package* — rejected. It is an application, not a library, so
  `[tool.uv] package = false` plus `pythonpath = ["."]` avoids a build backend entirely.
- *Document constructor arguments on `__init__`* — rejected. Google style puts them on the class, so
  `D107` is ignored rather than duplicating `Args:` in two places where they would drift.
- *`filterwarnings = ["error"]` in pytest* — deferred. Several dependencies emit import-time
  `DeprecationWarning`s on 3.12 that would fail collection before any test runs. Revisit with a
  targeted ignore list once CI exercises the full set.

**Verification:** `make lint` (ruff, ruff format, mypy --strict, ESLint, tsc, Prettier) and
`make test` both green. 106 backend tests at **100% line coverage** on every backend module, plus 6
frontend tests. Test isolation confirmed by running the suite in three orderings and each file alone.
Both servers start; the health endpoint answers; the Vite proxy reaches the backend.

**Smoke test found a real bug.** An unrelated PHP server on the development machine holds IPv6 port
8000. macOS resolves `localhost` to `::1` first and Node 17+ no longer reorders DNS results, so the
Vite proxy aimed at `localhost:8000` was silently reaching *that* server and returning HTML where the
app expected JSON. Pinned the proxy to the IPv4 literal with the reasoning recorded inline, since
this looks like needless specificity to anyone who has not hit it.

**Review.** Five independent lenses (backend Python, test quality, build/CI, frontend tooling, spec
drift) raised 39 findings; each was handed to a skeptic instructed to refute it. 19 survived and were
fixed; 20 were refuted, mostly for overstated impact rather than wrong observation. Findings worth
recording:
- `_load_settings` caught only `ValidationError`, but pydantic-settings raises `SettingsError` from
  inside the settings *source* when it cannot JSON-decode a complex field. A `CORS_ORIGINS` written in
  the format `.env.example` itself documented therefore crashed with a raw traceback instead of the
  actionable `ConfigError`. Fixed both the wrapper and the misleading documentation.
- `GROQ_API_KEY=` (exactly what `cp .env.example .env` leaves) parsed as `SecretStr("")`, so an
  `is None` fail-fast check would not fire and the failure would surface later as an opaque `401`.
  Now normalised to `None`.
- `LOG_LEVEL=NOTSET` was accepted. On the *root* logger NOTSET means level 0, which emits everything —
  more verbose than DEBUG, and enough to switch on the DEBUG transcript logging TR-181 gates. Removed
  from the allowed set, and its test moved from the accepted list to the rejected one.
- No coverage artefact was git-ignored, so the next `git add -A` would have committed `.coverage`, a
  binary SQLite file rewritten on every test run.
- `npm run typecheck` compiled only `tsconfig.app.json`, so `vite.config.ts` was type-checked by
  nothing. Switched to `tsc -b --noEmit` and proved the fix by injecting a type error and watching it
  fail.
- CI ran `uv sync` without `--frozen` while the frontend used `npm ci`, so only half the build honoured
  its lockfile. The frontend lockfile was also stale, still carrying `oxlint` and 20 platform binaries.
- The env-scrubbing test fixture deleted only the lower and upper spellings of each setting, but
  `case_sensitive=False` matches any casing, so a shell exporting `Groq_Api_Key` would have defeated
  the isolation. Now scans the real environment and deletes anything that case-folds to a field name.

**Two findings the skeptics refuted but I acted on anyway.** A verifier demonstrated that pointing the
frontend at `https://evil.example.com` left all three tests passing, then refuted the finding on the
grounds that the scenario could not occur in production. The mutation survival is the point: Phase 0
had just been bitten by exactly that bug class in the proxy. Rewrote the frontend tests and confirmed
by mutation that a wrong URL now fails. Likewise the "stale lockfile" refutation was right that both
install paths agree today, but a lockfile listing a removed dependency is still wrong, so it was
refreshed.

**Documentation corrected against the code:** the TRD's health-body contract was missing the `version`
field that the code, README, and tests all have; `logging_setup.py` existed in no document; CLAUDE.md
still said React 18 and described a plain-`logging` standard the implementation had already
outgrown; and every weekday label in the PRD milestone table was one day early (9 September 2026 is a
Wednesday).

**Follow-ups:**
- `eslint.config.js` lints `**/*.js` with Node globals, which will flag `AudioWorkletProcessor` when
  the capture worklet lands in Phase 2. Add a worklet-specific config block then.
- Enable `fail_under` for coverage once the pipeline modules exist (TRD §12.3).
- Revisit `filterwarnings = ["error"]` with a targeted ignore list.
- Confirm Kokoro's returned sample rate is 24,000 when weights download in Phase 2.

### 2026-09-10 · Toolchain probes close the two biggest unknowns · uncommitted
**Scope:** `docs/TRD.md` (TR-010, TR-083, TR-086, §2.2, §3.2, §16)
**Change:** Probed `kokoro-onnx` on Python 3.12 arm64 in a throwaway venv before committing to it, and
scaffolded the frontend to learn the template's real dependency versions. Folded both results into the TRD.
**Why:** TRD §16 listed Kokoro wheel availability as the top technical risk, due in M2. Finding out on
Wednesday that TTS does not install would have cost a day. The frontend probe was free (the scaffold had
to happen anyway) and changed a documented decision.
**Findings:**
- `kokoro-onnx` 0.6.1 installs cleanly with `onnxruntime` 1.29.0 and `numpy` 2.5.3. Import succeeds and
  `onnxruntime` reports a **CoreML** execution provider alongside CPU, so synthesis can use the Apple
  Silicon neural engine. Phonemisation ships via `espeakng-loader`, so no system `espeak` install is needed.
- **API correction.** `Kokoro.create(text, voice, speed, lang, ...)` returns `(float32 array, sample_rate)`
  for the whole utterance. It is not an incremental generator, so TR-083's claim that the first frame
  leaves before the sentence finishes synthesising was wrong. Rewrote TR-083 and added TR-086: time to
  first audio now depends on the `SentenceChunker` splitting early, which puts TR-042 on the critical path.
- The current Vite template ships React 19, TypeScript 6, Vite 8, and `oxlint` rather than the React 18 and
  ESLint the TRD assumed. Updated §2.2 and §3.2.
**Alternatives considered:**
- *Keep `oxlint`* (the template default, much faster) — rejected. It has no type-aware rules, and
  `no-floating-promises` / `no-misused-promises` / `await-thenable` guard exactly the code most likely to
  break here: playback teardown, `AudioContext` lifecycle, WebSocket handlers. One linter, chosen for the
  rules that matter, beats two linters or a fast one that misses the bug class.
- *Run both linters* — rejected as friction for no coverage gain.
- *Pin TypeScript to 5.9 for a supported `typescript-eslint` range* — deferred. TS 6 may emit an
  unsupported-version warning; if it hard-fails, pinning is the fallback and is a one-line change.
**Verification:** Probe venv imported `kokoro_onnx`, `onnxruntime`, `numpy`, `soundfile` and printed the
real `create()` signature. Frontend dependency install completed and versions were read back from
`package.json`.
**Follow-ups:** Confirm Kokoro's returned sample rate is 24,000 when weights are downloaded in Phase 2.
Confirm `typescript-eslint` 8.70 runs against TypeScript 6.0 during the Phase 0 smoke test.

## 2026-09-09

### 2026-09-09 · Project inception and documentation set · uncommitted
**Scope:** `docs/PRD.md`, `docs/TRD.md`, `docs/TEST_CASES.md`, `docs/EVALS.md`, `docs/ENGINEERING_LOG.md`, `CLAUDE.md`, `.gitignore`, `.env.example`
**Change:** Wrote the product requirements, technical requirements with full architecture, the test-case catalogue seeded with ~90 planned cases, the evaluation design, and this log. Set engineering standards in `CLAUDE.md`.
**Why:** Establish shared, reviewable intent before code. The system has several concurrent moving parts (VAD, cancellable async pipeline, gapless playback) where an unclear contract would be expensive to fix later.
**Alternatives considered:**
- *Speech-to-speech API (Gemini Live, OpenAI Realtime)* — faster to build, native barge-in, but turn-taking logic would be opaque and vendor-owned; Gemini Live is free but closed. Rejected in favour of an open-weight STT→LLM→TTS pipeline where every stage is inspectable and swappable.
- *Continuous audio streaming to the backend with server-side VAD* — lower theoretical latency (STT could start before the utterance ends) but multiplies STT requests, complicates cancellation, and adds a network hop to barge-in detection. Rejected for v0.1.0; browser VAD with utterance-level STT chosen. Revisit if `first_audio_ms` misses budget.
- *Next.js API routes as the backend* — single repo simplicity, but Python's async model and audio ecosystem fit the pipeline better. FastAPI chosen.
- *Kokoro via PyTorch package vs `kokoro-onnx`* — ONNX chosen for smaller dependency surface and CPU speed; the PyTorch package is the fallback if wheels fail.
**Verification:** Documents cross-reference each other by ID (F-, TR-, TC-, E-). No code yet.
**Follow-ups:** Verify `kokoro-onnx` installs on Python 3.12 arm64 at the start of M2. Decide default Kokoro voice by ear. Confirm Groq SSE tool-call delta format with a recorded fixture before writing the parser.
