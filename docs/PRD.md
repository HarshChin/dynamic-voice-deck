# Dynamic Voice Deck — Product Requirements Document

| Field | Value |
|---|---|
| Project | Dynamic Voice Deck |
| Owner | Harshvardhan |
| Target release | v0.1.0 — Friday, 11 September 2026 |
| Distribution | Public GitHub repository, runs locally |
| Related docs | `docs/TRD.md`, `docs/TEST_CASES.md`, `docs/EVALS.md`, `docs/ENGINEERING_LOG.md` |
| Status | Draft v1.1 — 9 September 2026 |

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

We build the voice loop as an explicit **STT → LLM → TTS pipeline from open-weight models**, rather than using a closed speech-to-speech API. This means the hardest parts of voice UX — voice activity detection, endpointing, barge-in, and history truncation — are implemented in this codebase and owned end to end. Every model is open weight; hosted inference (Groq free tier) is used for speed, with a flag to run fully local.

### 1.2 Distribution

v0.1.0 is distributed as source. The repository runs on a laptop with two commands (backend and frontend). Cloud deployment is out of scope for this release.

---

## 2. Goals and non-goals

### 2.1 Goals

| # | Goal | How we measure it |
|---|---|---|
| G1 | A user can hold a natural spoken conversation about the deck | Walkthrough scenario (§13) completes without a keyboard |
| G2 | Slides change automatically from questions, including non-literal ones ("go back to the part about cost") | ≥ 9 of 10 scripted routing questions land on the correct slide |
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

The default topic is the system itself. The agent explains its own architecture, and when a user asks "how do you handle me interrupting you?", it jumps to the barge-in slide while demonstrating it. Each slide has a title, 3–5 bullets, a set of **routing keywords/aliases**, and **speaker notes** that the agent uses as ground truth when presenting.

| # | Title | Bullets (summary) | Routing aliases |
|---|---|---|---|
| 1 | Anatomy of a Voice Agent | What this demo is; how to talk to it; what to try (interrupt me, ask to jump around) | intro, start, overview, beginning, what is this |
| 2 | The Latency Budget | Where the milliseconds go: VAD ~30 ms, STT ~300 ms, LLM time-to-first-token ~200 ms, TTS time-to-first-byte ~200 ms, network; why 1 s feels slow and 500 ms feels alive | latency, speed, delay, milliseconds, fast, slow, budget, response time |
| 3 | Hearing: VAD and Turn-Taking | What Silero VAD does; endpointing (how long silence means "done"); false triggers, background noise; why VAD runs in the browser | vad, hearing, listening, turn taking, when do you start talking, silence, microphone, noise, end of speech |
| 4 | Barge-in: Interrupting Gracefully | Two-tier cancellation (client stops audio, server cancels LLM/TTS); truncating history to what was actually heard; resuming | interrupt, interruption, barge in, cut off, stop talking, talk over, cancel |
| 5 | Thinking: Tool Calling and Intent Routing | How the LLM gets the deck; the `go_to_slide` tool; reason strings shown in the UI; fallback keyword routing; bidirectional sync | tools, tool calling, function calling, routing, how do you change slides, navigation, intent |
| 6 | Trade-offs and What's Next | Speech-to-speech vs pipeline; open weights vs hosted; what we would build with a week; cost | trade offs, next steps, future, roadmap, comparison, speech to speech, cost, conclusion, summary, end |

The deck is a JSON file (`backend/app/decks/anatomy_of_a_voice_agent.json`), validated by a Pydantic model. Additional decks can be dropped in the same folder.

---

## 5. System architecture

```
┌──────────────────────────── Browser (React + Vite + TS) ────────────────────────────┐
│  Mic ──► AudioWorklet (16 kHz PCM16) ──► Silero VAD (onnx, in-browser)              │
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
│      AudioBuffer ──► STTProvider ──► ConversationHistory ──► LLMProvider (stream,   │
│                       (Groq Whisper)                         tools)  (Groq gpt-oss) │
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
| **AudioCapture** (FE) | Requests mic permission, captures mono audio, resamples to 16 kHz PCM16 in an AudioWorklet, emits 20–40 ms frames. |
| **VAD** (FE) | Silero VAD via `@ricky0123/vad-web`. Emits `speech.start`, `speech.end` (with the buffered utterance), and `misfire`. Runs continuously while the session is live. |
| **PlaybackQueue** (FE) | Schedules 24 kHz PCM chunks on an AudioContext gaplessly; reports which sentence finished playing; can flush instantly. |
| **SessionClient** (FE) | Owns the WebSocket, encodes/decodes protocol messages (§9), reconnects once on drop. |
| **SlideDeck** (FE) | Renders the current slide, highlight state, progress dots, keyboard navigation. |
| **Orb** (FE) | Animated agent state indicator. |
| **EventLog** (FE) | Transcript plus tool-call and state chips. |
| **LatencyHUD** (FE) | Per-turn metrics. |
| **SessionManager** (BE) | Creates/destroys `Session` objects per WebSocket. |
| **Session** (BE) | State machine, conversation history, current pipeline task, cancellation token. |
| **STTProvider** (BE) | `transcribe(pcm16: bytes) -> Transcript`. Implementations: `GroqWhisperSTT`, `FasterWhisperSTT` (local, P2). |
| **LLMProvider** (BE) | `stream(messages, tools) -> AsyncIterator[Token | ToolCall]`. Implementations: `GroqLLM`, `OllamaLLM` (local, P2). |
| **TTSProvider** (BE) | `synthesize(text) -> AsyncIterator[bytes]` (24 kHz PCM16). Implementations: `KokoroTTS` (onnx, CPU). |
| **SentenceChunker** (BE) | Splits a token stream into speakable sentences/clauses for early TTS start. |
| **SlideController** (BE) | Validates tool calls against the deck, applies keyword-fallback routing, emits `slide.goto`. |
| **Metrics** (BE) | Timestamps every pipeline stage per turn and emits a `metrics` message. |

### 5.2 Technology choices

| Layer | Choice | Rationale |
|---|---|---|
| Backend | Python 3.12, FastAPI, uvicorn, asyncio | First-class WebSockets; asyncio cancellation maps directly to barge-in; strong audio/ML ecosystem. |
| STT | Whisper large-v3-turbo on Groq (open weights) | ~300 ms for a short utterance; free tier 20 RPM / 2,000 RPD. Local fallback: `faster-whisper`. |
| LLM | `openai/gpt-oss-120b` on Groq (open weights), fallback `llama-3.3-70b-versatile` | Native tool calling, streaming, ~200 ms TTFT; free tier 30 RPM. Local fallback: Ollama. |
| TTS | Kokoro-82M via `kokoro-onnx` (Apache 2.0) | Runs on CPU in ~real-time, no GPU, no account, ~1 GB RAM. |
| VAD | Silero VAD in-browser (`@ricky0123/vad-web`) | Zero network hop for barge-in detection; frees the backend from streaming raw audio continuously. |
| Frontend | React 18 + Vite + TypeScript, no UI framework, CSS modules | Lightweight, fast to iterate, no build-tool surprises. |
| Packaging | `uv` for Python, `npm` for the frontend, a root `Makefile` | Two-command start. |

---

## 6. Feature specifications

Each feature lists: description, user actions, expected behaviour, edge cases, acceptance criteria, and priority (**P0** must ship, **P1** should ship, **P2** stretch).

### F1. Slide deck rendering — P0

**Description.** Renders the active deck as full-width slides with title, bullets, optional highlighted bullet, and a slide counter.

**User actions and expected behaviour.**

| Action | Expected behaviour |
|---|---|
| Page loads | Slide 1 is shown. Deck title in the header. A "Start voice session" button is prominent. |
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
| Click "Start voice session" | Browser prompts for mic permission. On grant: WebSocket connects, `session.start` sent, orb shows *connecting* then *listening*. The agent greets in ≤ 2 s: one sentence welcome plus an invitation ("Ask me anything, or say 'start' and I'll walk you through it. Interrupt me any time."). |
| Mic permission denied | A clear inline error with instructions; text-input fallback (F13) is offered. No crash. |
| Click "End session" | Playback flushed, mic released, WebSocket closed with code 1000, orb returns to *idle*. Transcript remains visible. |
| Backend unreachable | Error toast "Backend not reachable at ws://localhost:8000" with a retry button. |
| WebSocket drops mid-session | One automatic reconnect attempt with a fresh session; log line shown. History is not preserved (non-goal). |

**Acceptance.** Full start/stop cycle repeatable five times without page reload or leaked audio nodes.

---

### F3. Voice activity detection and turn-taking — P0

**Description.** Determines when the user starts and stops speaking, in the browser, using Silero VAD.

**Parameters (tunable in `frontend/src/config.ts`).**

| Parameter | Default | Meaning |
|---|---|---|
| `positiveSpeechThreshold` | 0.6 | Probability above which a frame is speech |
| `negativeSpeechThreshold` | 0.35 | Probability below which a frame is silence |
| `redemptionMs` | 600 | Silence duration that ends an utterance (endpointing) |
| `minSpeechMs` | 250 | Utterances shorter than this are discarded as misfires |
| `preSpeechPadMs` | 300 | Audio kept from before speech onset so first syllables are not clipped |

**Expected behaviour.**

| Situation | Behaviour |
|---|---|
| User starts speaking while agent is *listening* | Orb shows *hearing* (subtle pulse). `speech.start` sent. |
| User stops for ≥ `redemptionMs` | `speech.end` sent with the complete utterance as PCM16. Orb shows *thinking*. |
| User speaks while agent is *speaking* | Barge-in path (F7). |
| Short noise (cough, keyboard) | Discarded; no server round trip; a faint "misfire" tick in the event log in debug mode. |
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

**Edge cases.** Groq rate limit or 5xx → one retry after 500 ms; on second failure, `error` message with `recoverable: true`, session returns to *listening*, orb flashes red once.

**Acceptance.** Transcripts of 10 scripted questions are accurate enough that routing (F5) succeeds on ≥ 9.

---

### F5. Agent reasoning and slide routing — P0

**Description.** The LLM answers the question in the voice of a presenter and navigates the deck through tool calls.

**Inputs to the model.**

- System prompt (see §8) including the full deck: slide indices, titles, bullets, speaker notes, and aliases.
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

**Expected behaviour.**

| User says | Expected agent behaviour |
|---|---|
| "How do you handle interruptions?" | Calls `go_to_slide(4, "User asked about interruption handling")` → slide 4 appears → agent explains in 2–4 sentences using slide 4's notes. |
| "Go back to the latency thing." | `go_to_slide(2, ...)` → brief recap of slide 2, not a full re-presentation. |
| "Next." / "Go on." | `go_to_slide(current + 1)` and presents that slide. On the last slide, says so and offers a summary. |
| "What's on this slide?" | No navigation; explains the current slide. |
| "What's the weather in London?" | No navigation; a one-sentence polite redirect back to the deck. No hallucinated tool call. |
| "Which slide talks about cost?" | `go_to_slide(6, ...)` because slide 6's aliases include cost; explains the cost trade-off. |

**Fallback routing.** If the LLM's answer clearly concerns a different slide but it did not call the tool (detected by the SlideController scoring the transcript against slide aliases with a confidence threshold), the controller emits `slide.goto` with reason `"Keyword match: <alias>"`. This is logged distinctly so the demo can show both paths.

**Constraints on the spoken answer.** 2–4 sentences unless asked for more. No markdown, no bullet symbols, no URLs (it will be spoken). Numbers spelled naturally.

**Metrics.** `llm_ttft_ms` (time to first token) and `llm_total_ms`.

**Acceptance.** The routing test set in `backend/tests/test_routing.py` (10 utterances → expected slide) passes ≥ 9/10 against the live provider (marked as an integration test), and 10/10 against the keyword fallback.

---

### F6. Streaming text-to-speech and playback — P0

**Description.** Converts the streamed answer to audio with minimal time-to-first-audio and plays it gaplessly.

**Pipeline behaviour.**

1. Tokens stream from the LLM into the SentenceChunker.
2. The chunker emits a segment at the first sentence terminator (`. ! ?`) or at a clause boundary (`, ; :`) once ≥ 60 characters are buffered, or at stream end. Segments get incrementing `sentence_id`s.
3. Each segment is synthesised by Kokoro in order (a bounded queue of 2 keeps memory flat and cancellation fast).
4. Audio is sent as binary WebSocket frames: 8-byte header (`sentence_id: uint32`, `seq: uint32`) + PCM16 24 kHz mono.
5. `transcript.agent` for each segment is sent when its first audio frame is sent, so captions track speech.
6. The client's PlaybackQueue schedules chunks back-to-back on the AudioContext clock and sends `playback.progress` when a `sentence_id` finishes.

**Expected behaviour.**

- Orb enters *speaking* on first audio frame and returns to *listening* when the last `sentence_id` finishes playing (client-driven, so the orb never lies about audio state).
- No audible gaps between sentences under normal Groq latency; if a chunk is late, the queue simply waits (a gap is preferable to a stutter).

**Metrics.** `tts_ttfb_ms` (first audio byte after first token) and `first_audio_ms` (end of user speech → first audio played, measured on the client).

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
| User interrupts, then says nothing (misfire) | If the utterance is discarded as a misfire, the client sends `interrupt.cancel`. Server has already cancelled; agent says "Sorry, go on — shall I continue?" **only if** the cancelled response had unsent content; otherwise stays quiet. |
| Agent is in *thinking* (no audio yet) and user speaks | Same cancellation path; nothing to truncate. |
| Two interruptions within 500 ms | Idempotent; second is ignored. |

**Instrumentation.** `interrupt_stop_ms` = speech onset (VAD timestamp) → last audio sample stopped, measured on the client and shown in the HUD. Target ≤ 150 ms; stretch ≤ 80 ms.

**Acceptance.** 10 interruptions in a row: audio always stops within 150 ms; the agent never repeats a sentence it already finished; the agent never references content it did not say. Verified by an automated backend test of history truncation plus a manual checklist.

---

### F8. Presentation flow and resume — P1

**Description.** The agent can present the deck end-to-end unattended, and can resume after a digression.

**Modes.**

- **Q&A mode (default on connect).** Agent greets and waits.
- **Presentation mode.** Triggered by the user saying "start", "present", "walk me through it", or clicking "Auto-present". The agent presents slide *n*, calls `go_to_slide(n+1)`, presents, and so on. Between slides it pauses ~700 ms (a natural breath), during which a user question is not an interruption but a normal turn.

**Resume logic.** The Session tracks two indices: `current_slide` (what is on screen) and `presentation_cursor` (where the walkthrough was). After a digression that navigated elsewhere, when the user says "continue", "go on", "where were we", the agent returns to `presentation_cursor` and picks up from the next unspoken bullet. If the user asks an unrelated question mid-presentation, the agent answers it, then asks once: "Shall I pick up from slide three?" and waits.

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
| Interrupt | Red chip: `⏹ interrupted after 2 sentences · stopped in 64 ms` |
| Error | Amber chip with message |

**Controls.** Toggle "debug" to show misfires and raw metrics. "Copy log" copies JSON of all events for bug reports and eval review.

**Acceptance.** Every server message type appears in the log in the correct order.

---

### F11. Agent state indicator (orb) — P0

**Description.** A single animated element communicating what the agent is doing, so users know when to speak.

| State | Visual | Audio cue |
|---|---|---|
| idle | Grey, static | — |
| connecting | Slow grey pulse | — |
| listening | Soft blue breathing | — |
| hearing (user speaking) | Blue, amplitude-reactive ring from mic level | — |
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
| `tts ttfb` | server | First segment sent to TTS → first audio bytes |
| `first audio` | client | VAD `speech.end` → first sample played |
| `interrupt stop` | client | VAD `speech.start` during playback → audio silent |

Shows the last turn and a rolling median across the session. Colour: green ≤ target, amber ≤ 1.5× target, red beyond.

---

### F13. Fallbacks and robustness — P1

| Fallback | Trigger | Behaviour |
|---|---|---|
| **Push-to-talk** | Toggle in settings, or hold `Space` | VAD disabled; audio captured while held; released → `speech.end`. Holding Space during playback still triggers barge-in. |
| **Text input** | Always visible below the transcript | `text.input {text}` skips STT and runs the rest of the pipeline. The agent answers with voice. Useful when the mic fails, in a noisy room, or for automated tests. |
| **Local providers** | `STT_PROVIDER=local`, `LLM_PROVIDER=ollama` | Swaps to faster-whisper and Ollama. Same interface; no code changes. (P2 to implement; P1 to keep the seams clean.) |
| **Provider failure** | Exception from any provider | `error` message, session returns to *listening*, orb amber; never a stuck *thinking* state. A 20 s pipeline watchdog enforces this. |
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
                                  │ speech.start                      │ playback finished
                                  ▼                                   │ (client reports last sentence)
                               HEARING                                │
                                  │ speech.end (utterance)            │
                                  ▼                                   │
                               THINKING ── first audio frame ──► SPEAKING
                                  │                                   │
                                  │ interrupt / speech.start          │ interrupt (speech.start while playing)
                                  ▼                                   ▼
                              INTERRUPTED ─── cancel + truncate ─────►┘ (→ HEARING)
```

Rules:

- Only one pipeline task exists per session at any time; starting a new one cancels the old one.
- Every transition emits a `state` message with a monotonic `turn_id` so the client can ignore stale messages from a cancelled turn.
- The watchdog moves THINKING/SPEAKING → LISTENING after 20 s with an `error`.

---

## 8. Agent behaviour specification

### 8.1 System prompt principles

1. **Role.** "You are the presenter of a slide deck. You speak; your words are converted to audio."
2. **Deck as ground truth.** The full deck JSON is embedded. Only claim things supported by speaker notes; say "that's not in this deck" otherwise.
3. **Navigate first, then speak.** When a question is best answered by another slide, call `go_to_slide` before answering. When continuing a presentation, call it for the next slide.
4. **Spoken style.** 2–4 sentences. Plain prose, no lists, no markdown, no emojis, no URLs. Natural contractions. Numbers as words when short.
5. **Interruption awareness.** History may contain `[interrupted by user]` markers. Never repeat content before the marker unless asked. Do not apologise more than once.
6. **Stay in scope.** Off-topic questions get a one-sentence redirect.
7. **State awareness.** The prompt includes `current_slide`, `presentation_cursor`, and `mode`.

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
| `speech.start` | `{client_ts}` | VAD onset. If server state is SPEAKING/THINKING this is also treated as `interrupt`. |
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
| `session.ready` | `{session_id, deck: Deck, providers: {stt, llm, tts}}` | |
| `state` | `{value: State, turn_id, server_ts}` | |
| `transcript.user` | `{turn_id, text, final: true}` | |
| `transcript.agent` | `{turn_id, sentence_id, text}` | Sent with first audio of that sentence. |
| `tool.call` | `{turn_id, name, args, source: "llm" \| "fallback"}` | |
| `slide.goto` | `{index, highlight?: number, reason}` | |
| binary | 8-byte header `uint32 sentence_id, uint32 seq` (little-endian) + PCM16 LE 24 kHz mono | Audio. |
| `agent.cancelled` | `{turn_id, truncated_at_sentence_id}` | |
| `metrics` | `{turn_id, stt_ms, llm_ttft_ms, llm_total_ms, tts_ttfb_ms}` | |
| `error` | `{code, message, recoverable: boolean}` | |

Message schemas are defined once in `backend/app/protocol.py` (Pydantic) and mirrored in `frontend/src/protocol.ts`. A test asserts the two lists of `type` strings match.

---

## 10. Non-functional requirements

| Area | Requirement |
|---|---|
| Latency | Targets in §2.1 G3/G4; every stage instrumented. |
| Rate limits | ≤ 1 STT and ≤ 1 LLM request per user turn. Errors from 429 surface as recoverable errors, never crashes. |
| Memory | Backend steady state ≤ 1.5 GB with Kokoro loaded. |
| Startup | Backend ready ≤ 10 s including Kokoro warm-up (a warm-up synthesis of "Ready." runs at boot). |
| Privacy | Audio is processed in memory and never written to disk. `.env` is git-ignored. No analytics. |
| Browser support | Chrome/Edge latest (primary), Safari best-effort (AudioWorklet supported; VAD wasm tested). |
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
│   │   ├── session.py         # Session, state machine, pipeline orchestration
│   │   ├── pipeline/
│   │   │   ├── chunker.py     # SentenceChunker
│   │   │   ├── history.py     # ConversationHistory + truncation
│   │   │   ├── slides.py      # SlideController + keyword fallback
│   │   │   └── metrics.py
│   │   ├── providers/
│   │   │   ├── base.py        # STTProvider, LLMProvider, TTSProvider protocols
│   │   │   ├── groq_stt.py
│   │   │   ├── groq_llm.py
│   │   │   ├── kokoro_tts.py
│   │   │   ├── local_stt.py   # faster-whisper (P2)
│   │   │   └── ollama_llm.py  # (P2)
│   │   ├── decks/
│   │   │   ├── models.py      # Deck / Slide pydantic models
│   │   │   └── anatomy_of_a_voice_agent.json
│   │   └── prompts/
│   │       └── presenter.md   # system prompt template
│   ├── tests/
│   └── evals/
│       ├── datasets/          # routing.jsonl, interruption.jsonl, style.jsonl
│       └── run_evals.py
└── frontend/
    ├── package.json
    ├── index.html
    └── src/
        ├── main.tsx
        ├── App.tsx
        ├── config.ts
        ├── protocol.ts
        ├── audio/            # capture worklet, playback queue, vad
        ├── session/          # SessionClient, state store
        └── components/       # SlideDeck, Orb, EventLog, LatencyHUD, Controls
```

---

## 12. Milestones

| When | Milestone | Definition of done | Status |
|---|---|---|---|
| Wed 9 Sep (eve) | **M1 Text loop** | Deck renders; WS session; `text.input` → LLM with tools → `slide.goto` + streamed text in log. No audio yet. | shipped 10 Sep |
| Thu 10 Sep (am) | **M2 Audio out** | Kokoro streaming, PlaybackQueue, orb states, `transcript.agent` captions, metrics. | shipped 11 Sep |
| Thu 10 Sep (pm) | **M3 Audio in + barge-in** | Speech detection, Groq STT, two-tier interrupt, history truncation, `interrupt_stop_ms` in HUD. **Core product complete.** | shipped 11 Sep |
| Fri 11 Sep (am) | **M4 Polish** | Presentation mode + resume, bidirectional sync, push-to-talk, text fallback, README with trade-offs, test suite and eval run green. | shipped 11 Sep |
| Fri 11 Sep (pm) | **M5 Release v0.1.0** | Repo public, `docs/EVALS.md` populated with the release eval run, engineering log complete. Stretch: deck generation. | in progress |

The plan slipped by about a day: M2 and M3 both landed on the 11th rather than the 10th. The time
went on two things that were not in the plan, and both are recorded in the engineering log: Silero
VAD could not be made to load under Vite and was replaced, and the first real spoken session
surfaced four defects that no test had covered.

---

## 13. End-to-end walkthrough scenario

This scenario is the release acceptance test. It is also mirrored as TC-E2E-001 in `docs/TEST_CASES.md`.

1. Open app → click Start → agent greets. *(F2)*
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
| Groq daily STT limit hit during development | Blocks testing | Text-input path for most iteration; local faster-whisper fallback. |
| LLM does not call the tool reliably | Slides do not move | Strong prompt + keyword fallback routing + routing test set; try `llama-3.3-70b-versatile` if `gpt-oss-120b` under-calls. |
| VAD false positives from agent's own audio (echo) | Self-interruption loop | Use `echoCancellation: true` in `getUserMedia`; raise the positive threshold while speaking; require ≥ 3 consecutive speech frames during playback. Headphones recommended in README. |
| Kokoro CPU speed on a contributor's laptop | Gaps between sentences | Bounded prefetch queue of 2 segments; Kokoro-82M is faster than real time on Apple Silicon and most x86 laptops. |
| Time | Missing P1 features | Milestones ordered so that M3 alone is a complete core product. |

---

## 15. Open questions

1. Voice choice for Kokoro (`af_heart` vs `am_michael` vs a British voice) — pick during M2 by ear.
2. Should the greeting auto-start presentation mode? Default: no; the agent invites the user to choose.
3. Default audio guidance in the README: recommend headphones, or tune echo suppression well enough for laptop speakers.
