# Dynamic Voice Deck — Product Requirements Document

| Field | Value |
|---|---|
| Project | Dynamic Voice Deck |
| Owner | Harshvardhan |
| Target release | v0.1.0 — Friday, 11 September 2026 |
| Distribution | Public GitHub repository, runs locally |
| Related docs | `docs/TRD.md`, `docs/TEST_CASES.md`, `docs/EVALS.md`, `docs/ENGINEERING_LOG.md` |
| Status | v1.2 — 13 September 2026. Reconciled claim by claim against the shipped code; where the build differs from the design, the text says so and points at the reason. |

---

## 1. Background

Slide decks are static, but the questions people ask about them are not. Dynamic Voice Deck is a voice-first presenter: an AI agent that walks an audience through a deck, answers spoken questions, jumps to the slide that best answers each question, and can be interrupted at any moment the way a human presenter can.

The product must:

1. Present a deck of 5–6 slides on a chosen topic.
2. Change slides automatically based on the user's spoken question.
3. Let the user interrupt the agent mid-sentence and recover gracefully.
4. Ship as a lightweight frontend and backend that anyone can run locally.

The bar for v0.1.0 is a voice experience that feels alive at the millisecond level, with the engineering behind it visible to anyone reading the code.

### 1.1 Key design decision

We build the voice loop as an explicit **STT → LLM → TTS pipeline from open-weight models**, rather than using a closed speech-to-speech API. This means the hardest parts of voice UX — voice activity detection, endpointing, barge-in, and history truncation — are implemented in this codebase and owned end to end. Every model is open weight. Hosted inference (Groq free tier) is used for speed; the model stage also runs locally on Ollama, either as the primary or as an automatic fallback when the free tier rate-limits (F13). Transcription is Groq-only in v0.1.0: the local seam exists and the implementation behind it does not.

### 1.2 Distribution

v0.1.0 is distributed as source. The repository runs on a laptop with two commands (backend and frontend). Cloud deployment is out of scope for this release.

---

## 2. Goals and non-goals

### 2.1 Goals

| # | Goal | How we measure it |
|---|---|---|
| G1 | A user can hold a natural spoken conversation about the deck | Walkthrough scenario (§13) completes without a keyboard |
| G2 | Slides change automatically from questions, including non-literal ones ("go back to the part about cost") | Eval suite E1: routing accuracy ≥ 90 % with false navigation ≤ 5 %. Measured at v0.1.0: 83.3 % and 0 % (`docs/EVALS.md`) |
| G3 | Interruption feels instant and the agent recovers gracefully | Audio stops ≤ 150 ms after the user starts speaking; agent does not repeat itself or claim to have said unheard content |
| G4 | Low perceived latency | Median time from end of user speech to first agent audio ≤ 1.5 s on Groq; p95 ≤ 2.5 s |
| G5 | Clean, readable, idiomatic code that a new contributor can run in five minutes | Fresh clone → running app in ≤ 5 commands; ruff and tsc pass clean |
| G6 | The system explains itself | Live latency HUD, event log, documented trade-offs in the README, eval results in `docs/EVALS.md` |

### 2.2 Non-goals

- Multi-user or concurrent sessions at scale.
- Authentication, persistence beyond the process lifetime, or a database.
- Mobile layout (desktop Chrome is the target; Safari best-effort).
- Multilingual support (English only).
- Production hardening (TLS, rate-limit handling beyond graceful errors).
- Pixel-perfect slide design; slides are clean but content-first.

---

## 3. Users

| Persona | Situation | Needs |
|---|---|---|
| **Audience member** | Sits in front of the deck and asks questions by voice. | Know when to speak. Get answers on the right slide. Be able to cut in without friction. |
| **Presenter / operator** | Runs the app for a room, possibly on a conference mic. | Fallbacks when audio misbehaves. Reliable slide routing. A view of what the agent is doing. |
| **Contributor / reviewer** | Clones the repo and reads the code. Has 10 minutes. | Understand the architecture at a glance. Run it. Trust the tests and evals. |

---

## 4. Default deck: "Anatomy of a Voice Agent"

The default topic is the system itself. The agent explains its own architecture, and when a user asks "how do you handle me interrupting you?", it jumps to the barge-in slide while demonstrating it. Each slide has a title, three to six bullets, a set of **routing keywords/aliases**, and **speaker notes** that the agent uses as ground truth when presenting.

| # | Title | Bullets (summary) | Routing aliases (a selection; the deck carries more) |
|---|---|---|---|
| 1 | Anatomy of a Voice Agent | What this demo is; how to talk to it; what to try (interrupt me, ask to jump around) | intro, start, overview, beginning, what is this |
| 2 | The Latency Budget | Where the milliseconds go: endpointing 600 ms, speech-to-text ~300 ms, first token 250 ms budgeted, first Kokoro chunk ~250 ms, a 1.5 s budget to first sound with 2.5 s at p95, and the measured first token beside it | latency, speed, delay, milliseconds, fast, slow, budget, response time |
| 3 | Hearing: VAD and Turn-Taking | How speech is detected in the browser (an energy threshold with hysteresis; Silero was the design, see F3); endpointing (how long silence means "done"); false triggers, background noise; why detection runs in the browser | vad, hearing, listening, turn taking, when do you start talking, silence, microphone, noise, end of speech |
| 4 | Barge-in: Interrupting Gracefully | Two-tier cancellation (client stops audio, server cancels LLM/TTS); truncating history to what was actually heard; resuming | interrupt, interruption, barge in, cut off, stop talking, talk over, cancel |
| 5 | Thinking: Tool Calling and Intent Routing | How the LLM gets the deck; the `go_to_slide` tool; reason strings shown in the UI; fallback keyword routing; bidirectional sync | tools, tool calling, function calling, routing, how do you change slides, navigation, intent |
| 6 | Trade-offs and What's Next | Speech-to-speech vs pipeline; open weights vs hosted; what we would build with a week; cost | trade offs, next steps, future, roadmap, comparison, speech to speech, cost, conclusion, summary, end |

The deck is a JSON file (`backend/app/decks/anatomy_of_a_voice_agent.json`), validated by a Pydantic model. Additional decks can be dropped in the same folder.

---

## 5. System architecture

```
┌──────────────────────────── Browser (React + Vite + TS) ────────────────────────────┐
│  Mic ──► AudioWorklet (16 kHz PCM16) ──► energy detector (in-browser)               │
│                                   │  speech.start / speech.end / audio frames       │
│  Slide renderer ◄── slide.goto    │                                                 │
│  Orb (state) ◄──── state          ▼                                                 │
│  Transcript ◄──── transcript.*   WebSocket  /ws/session                             │
│  Latency HUD ◄─── metrics         ▲                                                 │
│  Playback queue ◄─ audio.chunk    │  interrupt / playback.progress / slide.changed  │
└───────────────────────────────────┼─────────────────────────────────────────────────┘
                                    │
┌───────────────────────────────────▼──────────── FastAPI backend ────────────────────┐
│  SessionManager ── one Session per socket                                           │
│    Session state machine: IDLE → LISTENING → THINKING → SPEAKING → (INTERRUPTED)    │
│    Pipeline (async, cancellable):                                                   │
│      Utterance ──► STTProvider ──► ConversationHistory ──► LLMProvider (stream,     │
│                    (Groq Whisper)                        tools) (Groq qwen3.8-27b)  │
│                                            tool calls ──► SlideController ──► client│
│                                            tokens ──► SentenceChunker ──► TTSProvider│
│                                                                          (Kokoro)   │
│  Providers are interfaces; implementations selected by env vars.                    │
│  REST: GET /api/decks, GET /api/decks/{id}, GET /api/health                         │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

### 5.1 Components

| Component | Responsibility |
|---|---|
| **Microphone** (FE) | Requests mic permission; an AudioWorklet downmixes, resamples to 16 kHz PCM16 and reports per-frame loudness; speech is detected from that loudness with hysteresis (`frontend/src/audio/microphone.ts`). Emits `speech.start`, `speech.end` (with the buffered utterance) and `misfire`. Runs continuously while the session is live. |
| **PlaybackQueue** (FE) | Schedules 24 kHz PCM chunks on an AudioContext gaplessly; reports which sentence finished playing; can flush instantly. |
| **SessionClient** (FE) | Owns the WebSocket, encodes/decodes protocol messages (§9), reconnects once on drop. |
| **SlideDeck** (FE) | Renders the current slide, highlight state, progress dots, keyboard navigation. |
| **Orb** (FE) | Animated agent state indicator. |
| **EventLog** (FE) | Transcript plus tool-call and state chips. |
| **LatencyHUD** (FE) | Per-turn metrics. |
| **SessionManager** (BE) | Creates/destroys `Session` objects per WebSocket. |
| **Session** (BE) | State machine, conversation history, current pipeline task, cancellation token. |
| **STTProvider** (BE) | `transcribe(pcm16: bytes) -> Transcript`. One implementation ships: `GroqWhisperSTT`. `STT_PROVIDER=local` is reserved for a local Whisper and fails at startup saying so. |
| **LLMProvider** (BE) | `stream(messages, tools, tool_choice) -> AsyncIterator[LLMEvent]`. `GroqLLM` and `OllamaLLM` both subclass one OpenAI-compatible streaming parser; `FallbackLLM` wraps a pair of them for F13. |
| **TTSProvider** (BE) | `synthesize(text) -> AsyncIterator[bytes]` (24 kHz PCM16). Implementations: `KokoroTTS` (onnx, CPU). |
| **SentenceChunker** (BE) | Splits a token stream into speakable sentences/clauses for early TTS start. |
| **SlideController** (BE) | Validates tool calls against the deck, applies keyword-fallback routing, emits `slide.goto`. |
| **Metrics** (BE) | Timestamps every pipeline stage per turn and emits a `metrics` message. |

### 5.2 Technology choices

| Layer | Choice | Rationale |
|---|---|---|
| Backend | Python 3.12, FastAPI, uvicorn, asyncio | First-class WebSockets; asyncio cancellation maps directly to barge-in; strong audio/ML ecosystem. |
| STT | Whisper large-v3-turbo on Groq (open weights) | ~300 ms for a short utterance; free tier 20 RPM / 2,000 RPD. No local STT in v0.1.0. |
| LLM | `qwen/qwen3.8-27b` on Groq (open weights); `qwen2.5:7b` on Ollama as an opt-in automatic fallback when the free tier rate-limits (TR-085) | Native tool calling and streaming; 540 ms median first token measured (`docs/EVALS.md`). Chosen over `openai/gpt-oss-120b` on measured behaviour: that model produced no visible answer on most paraphrased questions. Free tier: 7,000 input tokens a minute, 200,000 tokens a day. |
| TTS | Kokoro-82M via `kokoro-onnx` (Apache 2.0) | Runs on CPU in ~real-time, no GPU, no account, ~1 GB RAM. |
| Speech detection | Energy threshold with hysteresis in an in-browser AudioWorklet (`frontend/src/audio/microphone.ts`) | Zero network hop for barge-in detection; no continuous audio stream to the backend. Designed as Silero VAD; replaced on 2026-09-11 when it could not be made to load under Vite (TR-110, README trade-offs). |
| Frontend | React 19 + Vite 8 + TypeScript 6, no UI framework, CSS modules | Lightweight, fast to iterate, no build-tool surprises. |
| Packaging | `uv` for Python, `npm` for the frontend, a root `Makefile` | Two-command start. |

---

## 6. Feature specifications

Each feature lists: description, user actions, expected behaviour, edge cases, acceptance criteria, and priority (**P0** must ship, **P1** should ship, **P2** stretch).

### F1. Slide deck rendering — P0

**Description.** Renders the active deck as full-width slides with title, points, optional highlight, and a slide counter.

**Changed 2026-09-11.** A slide may declare how its points are arranged rather than always listing them: `metrics` for a slide whose points are measurements, `split` for one with two or three sides, `flow` for a sequence. The arrangement is presentation only, and it is bound to the points by construction — every item in a figure references the bullets it presents, by index, and validation rejects a figure that omits, repeats or invents one. That is what keeps the screen and the agent's view of a slide the same thing: the model is given the bullets, so a point it can mention is a point the room can see, and `highlight_bullet(n)` finds its target in any arrangement by looking for the item that claims bullet `n`. A slide with no figure, or one whose arrangement a build does not recognise, renders as a plain list.

**User actions and expected behaviour.**

| Action | Expected behaviour |
|---|---|
| Page loads | Slide 1 is shown. Deck title in the header. A "Start session" button is prominent. |
| Press `→` / `←` or click the arrows | Slide changes with a 200 ms transition. A `slide.changed` message (source `user`) is sent if a session is live (see F9). |
| Click a progress dot | Jumps to that slide; same as above. |
| Server sends `slide.goto` | Slide changes with the same transition. The event log shows the reason string. If `highlight` is present, that bullet is emphasised for 4 s or until the next highlight. |

**Edge cases.** Out-of-range index from the server is clamped and logged as a warning. Rapid successive `slide.goto` messages are applied in order; the last one wins.

**Acceptance.** All six slides render from JSON; keyboard and mouse navigation work; highlight animation visible.

---

### F2. Voice session lifecycle — P0

**Description.** Establishes and tears down a voice session: microphone permission, WebSocket connection, model warm-up.

**User actions and expected behaviour.**

| Action | Expected behaviour |
|---|---|
| Click "Start session" | Browser prompts for mic permission. On grant: WebSocket connects, `session.start` is sent, the server replies `session.ready` with the deck, and the orb goes *connecting* → *listening*. **The agent does not greet.** The design had a spoken welcome; slide 1 says the same thing in writing and is already on screen, and a greeting spends a model call and two seconds before the user has asked anything, so it was dropped. |
| Mic permission denied | A clear inline error with instructions; text-input fallback (F13) is offered. No crash. |
| Click "End session" | Playback flushed, mic released, WebSocket closed with code 1000, orb returns to *idle*. Transcript remains visible. |
| Backend unreachable | The header health indicator reads `backend: unreachable` (polled over REST), and the Start button's attempt fails into the event log. There is no toast: the log is where every other event already is. |
| WebSocket drops mid-session | One automatic reconnect attempt with a fresh session. If that fails, an alert entry in the log names the close code and says to press *Start session* again; that button is the retry. History is not preserved (non-goal). |

**Acceptance.** Full start/stop cycle repeatable five times without page reload or leaked audio nodes.

---

### F3. Voice activity detection and turn-taking — P0

**Description.** Determines when the user starts and stops speaking, in the browser. The detector is an energy threshold with hysteresis on per-frame loudness reported by an AudioWorklet. The design specified Silero VAD; it could not be made to load under Vite and was replaced on 2026-09-11 with the same timings (TR-110, engineering log). Swapping a neural detector back in is a one-file change.

**Parameters (tunable in `frontend/src/config.ts`).**

| Parameter | Default | Meaning |
|---|---|---|
| `speechRms` | 0.02 | Per-frame loudness (RMS) above which a frame counts as speech |
| `silenceRms` | 0.012 | Loudness below which a frame counts as silence; the gap between the two is the hysteresis |
| `redemptionMs` | 600 | Silence duration that ends an utterance (endpointing) |
| `minSpeechMs` | 250 | Utterances shorter than this are discarded as misfires |
| `preSpeechPadMs` | 300 | Audio kept from before speech onset so first syllables are not clipped |
| `onsetFramesWhilePlaying` | 3 | Consecutive loud frames needed to declare onset while the agent is audible, so its own echo does not interrupt it |
| `onsetFramesWhileIdle` | 1 | Consecutive loud frames needed while nothing is playing |
| `maxUtteranceMs` | 20000 | An utterance that never ends is cut here, so barge-in is never left disabled |

**Expected behaviour.**

| Situation | Behaviour |
|---|---|
| User starts speaking while agent is *listening* | Orb shows *hearing* (subtle pulse). `speech.start` sent. |
| User stops for ≥ `redemptionMs` | `speech.end` sent with the complete utterance as PCM16. Orb shows *thinking*. |
| User speaks while agent is *speaking* | Barge-in path (F7). |
| Short noise (cough, keyboard) | Discarded in the browser: no server round trip and no entry in the event log. If an `interrupt` had already been sent for that onset, an `interrupt.cancel` follows it. (The design showed a misfire tick in debug mode; debug reveals raw metrics and client notices, and a tick for every keystroke proved to be noise.) |
| User pauses mid-sentence for < `redemptionMs` | Treated as the same utterance. |

**Design note.** The client only sends audio for a finished utterance, not a continuous stream. This keeps the backend simple and uses one STT request per turn, which fits Groq's free-tier limits. The trade-off (STT cannot begin before the user finishes) is documented on slide 2.

**Acceptance.** In a quiet room, 10 consecutive utterances are each detected as exactly one turn; no clipped first words.

---

### F4. Speech-to-text — P0

**Description.** Transcribes each finished utterance.

**Expected behaviour.**

- Backend receives the utterance, wraps it as a 16 kHz mono WAV in memory, and calls the STT provider.
- On success, `transcript.user` is sent with `final: true`, and the text appears in the event log.
- Empty or whitespace-only transcripts (breathing, noise) are dropped silently; the session returns to *listening*.
- Very low-confidence or hallucinated filler ("Thank you." on silence, a known Whisper artefact) is filtered by a small deny-list.

**Metrics.** `stt_ms` recorded from request start to transcript.

**Edge cases.** Groq rate limit or 5xx → one retry after 500 ms; on second failure an `error` message with `recoverable: true` (carrying `retry_after_s` when the provider gave one), the session returns to *listening*, and the failure appears as an amber chip in the event log plus, for a 429, a countdown chip in the header. The orb follows session state and simply returns to listening; its red flash is reserved for an interruption.

**Acceptance.** Transcripts of 10 scripted questions are accurate enough that routing (F5) succeeds on ≥ 9.

---

### F5. Agent reasoning and slide routing — P0

**Description.** The LLM answers the question in the voice of a presenter and navigates the deck through tool calls.

**Inputs to the model.**

- System prompt (see §8) including every slide's index, title and bullets, and the speaker notes **of the current slide only**. Aliases are never sent: they exist for the server-side keyword fallback, and the notes of six slides cost ~1,570 input tokens a turn against a free-tier ceiling of 8,000 a minute. The cost is that the agent must navigate before it can quote another slide in detail, which is the behaviour the prompt asks for anyway.
- Current slide index and presentation cursor (F8).
- Conversation history (truncated correctly after interruptions, F7).
- Tool definitions:

```json
{
  "name": "go_to_slide",
  "description": "Navigate the deck to the slide that best answers the user's question or continues the presentation. Call this BEFORE speaking about a slide's content.",
  "parameters": {
    "type": "object",
    "properties": {
      "slide_index": {"type": "integer", "minimum": 1, "maximum": 6},
      "reason": {"type": "string", "description": "One short clause shown in the UI, e.g. 'User asked about interruption handling'."}
    },
    "required": ["slide_index", "reason"]
  }
}
```

```json
{
  "name": "highlight_bullet",
  "description": "Emphasise one bullet on the current slide while you talk about it.",
  "parameters": {
    "type": "object",
    "properties": {"bullet_index": {"type": "integer", "minimum": 0}},
    "required": ["bullet_index"]
  }
}
```

The two schemas above are the shape, not the literal text: `pipeline/tools.py` builds them from the deck, so `maximum` is the slide count of whatever deck is loaded.

**Expected behaviour.**

| User says | Expected agent behaviour |
|---|---|
| "How do you handle interruptions?" | Calls `go_to_slide(4, "User asked about interruption handling")` → slide 4 appears → agent explains in 2–4 sentences using slide 4's notes. |
| "Go back to the latency thing." | `go_to_slide(2, ...)` → brief recap of slide 2, not a full re-presentation. |
| "Next." / "Go on." | `go_to_slide(current + 1)`, then **one short sentence naming the slide**, not a presentation of it. On the last slide, says so. |
| "What's on this slide?" | No navigation; explains the current slide. |
| "What's the weather in London?" | No navigation; a one-sentence polite redirect back to the deck. No hallucinated tool call. |
| "Which slide talks about cost?" | `go_to_slide(6, ...)` because slide 6's aliases include cost; explains the cost trade-off. |

**Fallback routing.** If the LLM's answer clearly concerns a different slide but it did not call the tool (detected by the SlideController scoring the transcript against slide aliases with a confidence threshold), the controller emits `slide.goto` with reason `"Keyword match: <alias>"`. This is logged distinctly so the demo can show both paths.

**Constraints on the spoken answer.** Length follows what was asked, and there are two cases. A *question* gets a short opener plus two or three sentences that answer it, under ninety words. A *request to move* ("go to slide two", "next", "back one") gets the navigation and **one short sentence naming where the deck now is**, because the room can read the slide and reciting it makes the wait longer for nothing. Ambiguous input is treated as a request to move. Measured against the hosted model on 2026-09-13: "can you go to the second slide?" answers in four words, "what's this slide about?" in three sentences. No markdown, no bullet symbols, no URLs (it will be spoken). Numbers spelled naturally.

**Metrics.** `llm_ttft_ms` (time to first token) and `llm_total_ms`.

**Acceptance.** Three layers, none of them a file called `test_routing.py`: the keyword fallback's scoring is unit-tested in `backend/tests/test_slides.py`; one live routing turn runs as an integration test (TC-INT-003); and the real measure is eval suite E1, eighteen utterances across six categories, recorded in `docs/EVALS.md` (83.3 % at v0.1.0, against a 90 % bar).

---

### F6. Streaming text-to-speech and playback — P0

**Description.** Converts the streamed answer to audio with minimal time-to-first-audio and plays it gaplessly.

**Pipeline behaviour.**

1. Tokens stream from the LLM into the SentenceChunker.
2. The chunker emits a segment at the first sentence terminator (`. ! ?`), or at a clause boundary (`, ; :` or an em dash) once more than 60 characters are buffered and at least 24 of them precede the boundary, or at the last whitespace past 200 characters, or at stream end. Segments get incrementing `sentence_id`s.
3. Each segment is synthesised by Kokoro in order (a bounded queue of 2 keeps memory flat and cancellation fast).
4. Audio is sent as binary WebSocket frames: 8-byte header (`sentence_id: uint32`, `seq: uint32`) + PCM16 24 kHz mono.
5. `transcript.agent` for each segment is sent just before that segment is handed to synthesis, so the caption is on screen as the audio starts rather than after it.
6. The client's PlaybackQueue schedules chunks back-to-back on the AudioContext clock and sends `playback.progress` when a `sentence_id` finishes.

**Expected behaviour.**

- Orb enters *speaking* when the first audio frame leaves the server, and returns to *listening* when generation finishes. Synthesis outruns playback, so the room is often still hearing the answer at that moment; the session keeps a `_playing` flag until the client reports the final sentence played, and an `interrupt` arriving in that window is still honoured (TR-090).
- No audible gaps between sentences under normal Groq latency; if a chunk is late, the queue simply waits (a gap is preferable to a stutter).

**Metrics.** `tts_ttfb_ms` (first sentence handed to synthesis → its first audio bytes) and `first_audio_ms` (end of user speech → first audio played, measured on the client).

**Acceptance.** Median `first_audio_ms` ≤ 1.5 s across the demo script; agent voice is intelligible and not robotic; no clicks between segments.

---

### F7. Barge-in (interruption) — P0

**Description.** The user can speak over the agent at any time; the agent stops within ~100 ms, discards what it had not said, and answers the new input.

**Two-tier design.**

| Tier | Where | Trigger | Action | Target latency |
|---|---|---|---|---|
| 1 | Client | VAD `speech.start` while PlaybackQueue is playing | Flush the playback queue immediately (stop all scheduled sources). Record `last_completed_sentence_id`. Send `interrupt {last_completed_sentence_id}`. Orb shows *interrupted* flash then *hearing*. | ≤ 100 ms from speech onset |
| 2 | Server | `interrupt` received | Cancel the pipeline task (asyncio cancellation propagates to LLM stream and TTS jobs). Drop queued, unsent audio. Truncate the in-progress assistant message to the segments with `sentence_id ≤ last_completed_sentence_id`, append the marker `"[interrupted by user]"`. Send `agent.cancelled {truncated_at}`. State → *listening*. | ≤ 50 ms after receipt |

**Expected behaviour.**

| Scenario | Behaviour |
|---|---|
| User interrupts with a new question | Audio stops; new question is transcribed and answered normally. History contains only what the user actually heard, so the model does not think it already covered points it never spoke. |
| User interrupts with "stop" / "okay" / "got it" | Audio stops. Agent replies briefly ("Sure.") and waits. No re-presentation. |
| User interrupts, then says nothing (misfire) | The client sends `interrupt.cancel`. The server does nothing with it: the turn is already cancelled and the session is already listening, which is where a misfire should leave it. The design had the agent offer to continue ("shall I carry on?"); in practice that turns a cough into a conversation. |
| Agent is in *thinking* (no audio yet) and user speaks | **Onset alone does not cancel.** Nothing has been said, so there is nothing to cut short, and cancelling throws away work the user is waiting for. Observed live: the tail of the user's own sentence arrived just after their turn started and killed it twice. The utterance that follows supersedes the running turn anyway, which is the same cancellation taken where it is known to be wanted. An explicit `interrupt` message does cancel in *thinking*. |
| Two interruptions within 500 ms | Absorbed rather than re-run: within that window a second `interrupt` naming the same turn refines where history was cut if it carries a better `last_completed_sentence_id`, and otherwise does nothing. A barge-in on a *newly started* turn is always honoured, however soon it arrives. |

**Instrumentation.** `interrupt_stop_ms` is measured on the client as the duration of `PlaybackQueue.flush()` — the call that stops every scheduled source — and shown in the HUD. Target ≤ 150 ms.

**Acceptance.** 10 interruptions in a row: audio always stops within 150 ms; the agent never repeats a sentence it already finished; the agent never references content it did not say. Verified by an automated backend test of history truncation plus a manual checklist.

---

### F8. Presentation flow and resume — P1

**Description.** The agent can present the deck end-to-end unattended, and can resume after a digression.

**Modes.**

- **Q&A mode (default on connect).** The session opens listening. Nothing is spoken until the user speaks.
- **Presentation mode.** Triggered by the *Walk me through it* button or by saying one of six phrases matched on the server: "walk me through", "give me the tour", "present the deck", "start the presentation", "run through the deck", "take me through". Bare "start" or "present" go to the model like any other question. The walkthrough involves **no model call at all**: the server reads each slide's speaker notes through synthesis and moves the deck itself, which is why it costs nothing against the free tier. Between slides it pauses ~700 ms; speaking in that pause is a barge-in like any other and ends the tour, which is what the resume phrases below are for.

**Resume logic.** The Session tracks two indices: `current_slide` (what is on screen) and `presentation_cursor` (where the walkthrough was). Five phrases resume it, matched only while a walkthrough was what got interrupted: "carry on", "continue where we/you left off", "pick up where we/you left off", "where were we/you", "resume the tour/walkthrough/presentation". Bare "continue" and "go on" belong to the model, because in an ordinary exchange they ask for more of the answer. Resuming re-reads the cursor slide's notes from the top rather than hunting for the next unspoken bullet, and the agent never asks whether to resume: it waits to be asked.

**Acceptance.** Full auto-presentation completes across all six slides; interrupt on slide 3 with a question about slide 5; agent answers on slide 5; on "continue", returns to slide 3 and resumes.

---

### F9. Bidirectional sync (manual navigation informs the agent) — P1

**Description.** If the user navigates with keyboard or mouse, the agent's context is updated so it talks about what is on screen.

**Expected behaviour.**

- `slide.changed {index, source: "user"}` is sent to the server.
- Server updates `current_slide` and appends a system note to history: `"[User manually moved to slide 4: Barge-in]"`.
- If the agent is currently speaking about a different slide, it is **not** interrupted (the user may just be peeking). Its next answer, however, reflects the new slide.
- Saying "what's this?" after manual navigation explains the on-screen slide.

**Acceptance.** Manual jump to slide 6, then "explain this" → agent explains slide 6 without calling `go_to_slide`.

---

### F10. Event log and transcript panel — P0

**Description.** A right-hand panel that makes the system legible to operators and contributors.

**Entries.**

| Type | Rendering |
|---|---|
| User transcript | Right-aligned bubble |
| Agent transcript | Left-aligned bubble, appended sentence by sentence as audio plays; struck-through segments if cancelled before being heard |
| Tool call | Chip: `→ Slide 4 · User asked about interruption handling` |
| Keyword fallback | Chip in a different colour: `→ Slide 2 · keyword: latency` |
| State change | Thin divider: `listening → thinking (312 ms)` |
| Interrupt | Red chip: `Interrupted after 2 sentences`. The stop time is a HUD row, not a chip. |
| Error | Amber chip with message |

**Controls.** Toggle "debug" to reveal the entries hidden during a demo: per-turn metrics and client-side notices. An alert notice (a failed reconnect, say) is shown whether or not debug is on, because the user has to act on it. "Copy log" copies JSON of all events for bug reports and eval review.

**Acceptance.** Every server message type appears in the log in the correct order.

---

### F11. Agent state indicator (orb) — P0

**Description.** A single animated element communicating what the agent is doing, so users know when to speak.

| State | Visual | Audio cue |
|---|---|---|
| idle | Grey, static | — |
| connecting | Slow grey pulse | — |
| listening | Soft blue breathing | — |
| hearing (user speaking) | Blue, with a fixed sonar ring (1.1 s). The only live level the orb reads is the *output* level while speaking. | — |
| thinking | Violet, orbiting particles | — |
| speaking | Green, amplitude-reactive from output level | — |
| interrupted | Single red flash (150 ms) then → hearing | Very short soft tick (optional, off by default) |
| error | Amber outline | — |

The label under the orb states the mode in words for accessibility.

---

### F12. Latency HUD — P1

**Description.** A compact corner panel with per-turn timings so latency is always visible while developing and presenting.

| Metric | Source | Definition |
|---|---|---|
| `stt` | server | Utterance received → transcript |
| `llm ttft` | server | LLM request → first token |
| `llm total` | server | LLM request → last token |
| `tts ttfb` | server | First segment sent to TTS → first audio bytes |
| `first audio` | client | VAD `speech.end` → first sample played |
| `interrupt stop` | client | VAD `speech.start` during playback → audio silent |

Shows the last turn and a rolling median across the session. Colour: green ≤ target, amber ≤ 1.5× target, red beyond.

---

### F13. Fallbacks and robustness — P1

| Fallback | Trigger | Behaviour |
|---|---|---|
| **Push-to-talk** | The *Push to talk* button in the control bar, then hold `Space` | Automatic detection is disabled; audio is captured while the key is held; releasing it ends the utterance. Space does nothing unless the toggle is on and a session is live. Holding Space during playback still triggers barge-in. |
| **Text input** | Always visible below the transcript | `text.input {text}` skips STT and runs the rest of the pipeline. The agent answers with voice. Useful when the mic fails, in a noisy room, or for automated tests. |
| **Local model** | `LLM_PROVIDER=ollama`, or `LLM_FALLBACK_PROVIDER=ollama` | Shipped. As the primary it answers every turn locally; as the fallback it answers only when the hosted model is rate-limited, and the UI names both models and counts down to the hosted one's return. Measured against the same evals as the hosted model (`docs/EVALS.md`). |
| **Local speech-to-text** | `STT_PROVIDER=local` | Not built in v0.1.0. The seam exists and the settings model accepts the value, but the registry refuses it at startup with a message saying so, rather than failing at the first utterance. |
| **Provider failure** | Exception from any provider | `error` message with a code naming the stage, an amber chip in the log, session returns to *listening*; never a stuck *thinking* state. A 20 s pipeline watchdog enforces this. |
| **Mute** | Button | Stops sending audio; VAD paused; agent can still speak. |

---

### F14. Deck generation from a topic — P2 (stretch)

**Description.** A text box on the start screen: "Present about…". The backend asks the LLM for a six-slide deck in the deck JSON schema (structured output), validates it, and starts the session with it.

**Behaviour.** Generation ≤ 8 s with a progress state; on validation failure retry once, then fall back to the default deck with a notice. Generated decks are held in memory for the session only.

**Acceptance.** "Present about the history of jazz" yields a coherent six-slide deck and the agent presents it.

---

## 7. Session state machine

```
             session.start
   IDLE ─────────────────────► LISTENING ◄────────────────────────────┐
                                  │ speech.start                      │ generation finished
                                  ▼                                   │ (metrics sent; the room
                                                                      │  may still be hearing it)
                               HEARING                                │
                                  │ speech.end (utterance)            │
                                  ▼                                   │
                               THINKING ── first audio frame ──► SPEAKING
                                  │                                   │
                                  │ explicit interrupt only           │ interrupt (speech.start while playing)
                                  ▼                                   ▼
                              INTERRUPTED ─── cancel + truncate ─────►┘ (→ HEARING)
```

Rules:

- Only one pipeline task exists per session at any time; starting a new one cancels the old one.
- Every transition emits a `state` message with a monotonic `turn_id` so the client can ignore stale messages from a cancelled turn.
- LISTENING is entered when generation finishes, not when playback does. The client's `playback.progress` for the final sentence clears the session's `_playing` flag; until then an `interrupt` naming that turn still truncates history and emits `agent.cancelled` (TR-090).
- Voice onset while THINKING is not an interrupt (F7). The utterance that follows supersedes the running turn instead.
- The watchdog moves THINKING/SPEAKING → LISTENING after 20 s with an `error`.

---

## 8. Agent behaviour specification

### 8.1 System prompt principles

1. **Role.** "You are the presenter of a slide deck. You speak; your words are converted to audio."
2. **Deck as ground truth.** Every slide's title and bullets are embedded, with the speaker notes of the slide on screen. Only claim things supported by them; say "that's not in this deck" otherwise.
3. **Navigate first, then speak.** When a question is best answered by another slide, call `go_to_slide` before answering. When continuing a presentation, call it for the next slide.
4. **Spoken style.** A question gets two to four sentences; a bare request to move gets one. Plain prose, no lists, no markdown, no emojis, no URLs. Natural contractions. Numbers as words when short.
5. **Interruption awareness.** History may contain `[interrupted by user]` markers. Never repeat content before the marker unless asked. Do not apologise more than once.
6. **Stay in scope.** Off-topic questions get a one-sentence redirect.
7. **State awareness.** The prompt includes `current_slide` and its title, `presentation_cursor` and its title, `slide_count`, and `mode`.

### 8.2 Conversation history policy

- Messages: `system`, `user`, `assistant`, `tool`. Assistant messages that were cut off are stored as the heard prefix + `" [interrupted by user]"`.
- History is capped at the last 20 turns; older turns are dropped (no summarisation; non-goal).
- Manual navigation events are appended as short `system` notes.

---

## 9. WebSocket protocol

Endpoint: `ws://localhost:8000/ws/session`. Text frames carry JSON `{ "type": string, ... }`. Binary frames carry audio.

### 9.1 Client → Server

| type | Payload | Notes |
|---|---|---|
| `session.start` | `{deck_id: string, mode: "qa" \| "present", client_ts: number}` | First message. |
| `speech.start` | `{client_ts}` | Voice onset. Treated as an interrupt only while the server is SPEAKING; in THINKING it is not (F7). |
| `speech.end` | `{client_ts, duration_ms}` followed by **one binary frame**: PCM16 LE 16 kHz mono utterance | Server starts the pipeline on the binary frame. |
| `interrupt` | `{last_completed_sentence_id: number \| null, client_ts}` | Explicit barge-in with playback position. |
| `interrupt.cancel` | `{}` | The onset was a misfire. |
| `playback.progress` | `{sentence_id, turn_id}` | Sentence fully played. |
| `slide.changed` | `{index, source: "user"}` | Manual navigation. |
| `text.input` | `{text}` | Bypasses STT. |
| `control` | `{action: "start_presentation" \| "pause" \| "resume" \| "mute" \| "unmute"}` | |

### 9.2 Server → Client

| type | Payload | Notes |
|---|---|---|
| `session.ready` | `{session_id, protocol_version, deck: Deck, providers: {stt, llm, tts}}` | |
| `state` | `{value: State, turn_id, server_ts}` | |
| `transcript.user` | `{turn_id, text, final: true}` | |
| `transcript.agent` | `{turn_id, sentence_id, text}` | Sent with first audio of that sentence. |
| `tool.call` | `{turn_id, name, args, source: "llm" \| "fallback"}` | |
| `slide.goto` | `{turn_id, index, highlight?: number, reason}` | |
| binary | 8-byte header `uint32 sentence_id, uint32 seq` (little-endian) + PCM16 LE 24 kHz mono | Audio. |
| `agent.cancelled` | `{turn_id, truncated_at_sentence_id}` | |
| `metrics` | `{turn_id, stt_ms, llm_ttft_ms, llm_total_ms, tts_ttfb_ms, sentences}` | |
| `provider.fallback` | `{turn_id, stage: "llm", from_model, to_model, reason, retry_after_s?}` | The hosted model was rate-limited and a local one answered (F13). |
| `error` | `{code, message, recoverable: boolean, retry_after_s?}` | |

Message schemas are defined once in `backend/app/protocol.py` (Pydantic) and mirrored in `frontend/src/protocol.ts`. A test asserts the two lists of `type` strings match.

---

## 10. Non-functional requirements

| Area | Requirement |
|---|---|
| Latency | Targets in §2.1 G3/G4; every stage instrumented. |
| Rate limits | ≤ 1 STT request per user turn, and ≤ 3 LLM requests: one to answer, a second because a tool call returns no words, and a third only for the model that calls a second tool instead of speaking. A turn that navigates costs two. Errors from 429 surface as recoverable errors with a countdown, never crashes. |
| Memory | Backend steady state ≤ 1.5 GB with Kokoro loaded. |
| Startup | Backend ready ≤ 10 s including Kokoro warm-up (a warm-up synthesis of "Ready." runs at boot). |
| Privacy | Audio is processed in memory and never written to disk. `.env` is git-ignored. No analytics. |
| Browser support | Chrome/Edge latest (primary), Safari best-effort (AudioWorklet supported; the detector needs no WebAssembly). |
| Accessibility | Orb state also shown as text; all controls keyboard-reachable. |
| Code quality | See `CLAUDE.md`: ruff (lint + format), Google docstrings, type hints, ESLint + strict TS. |
| Tests and evals | Test catalogue in `docs/TEST_CASES.md`; agent evals in `docs/EVALS.md`. Every feature ships with its tests; every release ships with an eval run. |

---

## 11. Repository layout

```
dynamic-voice-deck/
├── README.md                  # quick start, architecture, trade-offs, GIF walkthrough
├── CLAUDE.md                  # engineering standards for AI-assisted work
├── Makefile                   # make setup / make backend / make frontend / make test / make lint
├── .env.example
├── docs/
│   ├── PRD.md                 # this document
│   ├── TRD.md                 # technical requirements and architecture
│   ├── TEST_CASES.md          # living test-case catalogue
│   ├── EVALS.md               # eval design and results per release
│   └── ENGINEERING_LOG.md     # dated record of changes and reasoning
├── backend/
│   ├── pyproject.toml         # uv-managed; ruff config
│   ├── app/
│   │   ├── main.py            # FastAPI app, routes, lifespan (model warm-up)
│   │   ├── config.py          # pydantic-settings
│   │   ├── protocol.py        # WS message models
│   │   ├── session.py         # Session, state machine, orchestration
│   │   ├── errors.py          # AppError hierarchy and error codes
│   │   ├── logging_setup.py   # structlog configuration
│   │   ├── pipeline/
│   │   │   ├── turn.py        # run_turn, run_presentation, SpeechSender
│   │   │   ├── chunker.py     # SentenceChunker
│   │   │   ├── history.py     # ConversationHistory + truncation
│   │   │   ├── slides.py      # SlideController + keyword fallback
│   │   │   ├── prompt.py      # PromptBuilder
│   │   │   ├── tools.py       # tool schemas, built from the deck
│   │   │   └── metrics.py
│   │   ├── providers/
│   │   │   ├── base.py        # STTProvider, LLMProvider, TTSProvider protocols
│   │   │   ├── openai_compat.py  # shared SSE streaming + tool-call assembly
│   │   │   ├── groq_stt.py
│   │   │   ├── groq_llm.py
│   │   │   ├── ollama_llm.py
│   │   │   ├── fallback.py    # FallbackLLM (F13)
│   │   │   ├── kokoro_tts.py
│   │   │   └── registry.py    # build_providers(settings)
│   │   ├── decks/
│   │   │   ├── models.py      # Deck / Slide / Figure pydantic models
│   │   │   ├── repository.py  # DeckRepository
│   │   │   └── anatomy_of_a_voice_agent.json
│   │   └── prompts/
│   │       └── presenter.md   # system prompt template
│   ├── tests/
│   └── evals/
│       ├── datasets/          # routing, interruption, grounded, judge_calibration (.jsonl)
│       ├── judges/            # rubric prompts
│       ├── results/           # recorded runs, JSON + Markdown
│       ├── harness.py, suites.py, judge.py, report.py, pacing.py, budget.py
│       └── run_evals.py
└── frontend/
    ├── package.json
    ├── index.html
    └── src/
        ├── main.tsx
        ├── App.tsx
        ├── config.ts
        ├── protocol.ts
        ├── keyboard.ts, time.ts
        ├── store.ts          # zustand store
        ├── audio/            # microphone.ts, playback.ts
        ├── session/          # client.ts, useSession.ts, usePushToTalk.ts
        └── components/       # SlideDeck, Slide, ProgressDots, Orb, EventLog,
                              # LatencyHUD, Controls, FallbackBanner, RateLimitChip
```

---

## 12. Milestones

| When | Milestone | Definition of done | Status |
|---|---|---|---|
| Wed 9 Sep (eve) | **M1 Text loop** | Deck renders; WS session; `text.input` → LLM with tools → `slide.goto` + streamed text in log. No audio yet. | shipped 10 Sep |
| Thu 10 Sep (am) | **M2 Audio out** | Kokoro streaming, PlaybackQueue, orb states, `transcript.agent` captions, metrics. | shipped 11 Sep |
| Thu 10 Sep (pm) | **M3 Audio in + barge-in** | Speech detection, Groq STT, two-tier interrupt, history truncation, `interrupt_stop_ms` in HUD. **Core product complete.** | shipped 11 Sep |
| Fri 11 Sep (am) | **M4 Polish** | Presentation mode + resume, bidirectional sync, push-to-talk, text fallback, README with trade-offs, test suite and eval run green. | shipped 11 Sep |
| Fri 11 Sep (pm) | **M5 Release v0.1.0** | Repo public, `docs/EVALS.md` populated with the release eval run, engineering log complete. Stretch: deck generation. | shipped 11 Sep; the release eval landed that evening after two attempts spent the free tier. Stretch (F14) not built. |

The plan slipped by about a day: M2 and M3 both landed on the 11th rather than the 10th. The time
went on three things that were not in the plan, all recorded in the engineering log. Silero VAD
could not be made to load under Vite and was replaced. The first real spoken session surfaced
four defects that no test had covered. And the release eval could not be run on the day it was
meant to gate, because one routing set cost a whole day of the free tier: the suites were resized
and the runner taught to stop when the budget does.

---

## 13. End-to-end walkthrough scenario

This scenario is the release acceptance test. It is also mirrored as TC-E2E-001 in `docs/TEST_CASES.md`.

1. Open app → click Start → the orb settles on *listening*. Nothing is spoken; slide 1 says what to try. *(F2)*
2. Say "Walk me through it." → agent presents slide 1, advances to slide 2. *(F8)*
3. Interrupt mid-sentence on slide 2: "Wait — how do you know when I've stopped talking?" → audio stops instantly, slide 3 appears, agent explains endpointing. HUD shows interrupt-stop time. *(F7, F5)*
4. Say "And what happens when I cut you off like that?" → slide 4, explanation. *(F5)*
5. Press `→` manually to slide 6, say "What's the catch with this approach?" → agent explains trade-offs without navigating. *(F9)*
6. Say "Okay, continue where we left off." → returns to slide 3 and resumes. *(F8)*
7. Type a question in the text box → voice answer. *(F13)*
8. Open the event log and confirm tool-call chips and, if triggered, the keyword-fallback chip are present. *(F10)*

---

## 14. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Kokoro Python packaging on the dev machine (Python 3.14 installed) | Blocks TTS | Pin Python 3.12 via `uv`; `kokoro-onnx` has minimal deps (onnxruntime, numpy). |
| Groq daily limits hit during development | Blocks testing | Materialised, on the model rather than on STT, and twice on release day. Mitigations that worked: the text-input path and the `fake` providers for most iteration, a local model on Ollama for the product, and eval suites sized to a day's budget. The planned local-Whisper fallback was never built. |
| LLM does not call the tool reliably | Slides do not move | Strong prompt + keyword fallback routing + routing eval set. Materialised the other way round: `gpt-oss-120b` called the tool but often produced no words, and the default moved to `qwen/qwen3.8-27b` on the measured comparison (`docs/EVALS.md`). |
| Speech detector triggering on the agent's own audio (echo) | Self-interruption loop | `echoCancellation: true` in `getUserMedia`, and onset requires ≥ 3 consecutive loud frames while the agent is audible against 1 while idle. The threshold itself does not change. Headphones recommended in the README. |
| Kokoro CPU speed on a contributor's laptop | Gaps between sentences | Bounded prefetch queue of 2 segments; Kokoro-82M is faster than real time on Apple Silicon and most x86 laptops. |
| Time | Missing P1 features | Milestones ordered so that M3 alone is a complete core product. |

---

## 15. Open questions — answered at v0.1.0

1. **Voice choice for Kokoro.** `af_heart`, picked by ear during M2 and set in the deck and in `.env.example`.
2. **Should the greeting auto-start presentation mode?** Moot: there is no greeting (F2). The walkthrough starts only when asked, by button or by phrase.
3. **Audio guidance.** Headphones are recommended in the README. Echo suppression plus the three-frame onset rule make laptop speakers usable, not reliable.
