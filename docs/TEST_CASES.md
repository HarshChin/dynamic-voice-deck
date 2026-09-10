# Test Case Catalogue

Living document. Every behavioural change adds or updates an entry here **in the same commit**. IDs are stable; never reuse a retired ID. Status values: `planned`, `implemented`, `passing`, `failing`, `retired`.

Columns: **ID** · **Feature / TR** (PRD feature and TRD requirement it verifies) · **Layer** · **Given / When / Then** · **Test location** · **Status**.

Naming: `TC-<layer>-<nnn>` where layer ∈ `BE` (backend unit/contract), `INT` (backend integration, needs key), `FE` (frontend unit), `PAR` (protocol parity), `E2E` (Playwright), `MAN` (manual checklist).

---

## Backend unit and contract

### Deck and prompt

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-BE-001 | F1 / TR-150 | Given the default deck JSON, when loaded, then it validates with 6 slides, contiguous indices, unique aliases | `tests/decks/test_repository.py` | planned |
| TC-BE-002 | TR-150 | Given a deck with duplicate aliases across slides, when loaded, then `DeckError` names both slides | same | planned |
| TC-BE-003 | TR-150 | Given a deck with 4 slides, when loaded, then validation fails (min 5) | same | planned |
| TC-BE-004 | TR-070/071 | Given a deck whose notes contain `{braces}`, when the prompt is built, then no exception and braces are preserved | `tests/pipeline/test_prompt.py` | planned |
| TC-BE-005 | TR-071 | Given notes of 1,000 chars, when the prompt is built, then the notes are truncated to 600 chars with an ellipsis | same | planned |
| TC-BE-006 | TR-070 | Given current_slide=3, cursor=2, mode=present, when built, then all three appear in the system prompt | same | planned |

### SentenceChunker

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-BE-010 | F6 / TR-040 | Given tokens forming "Hello there. How are you?", when fed token by token, then segments are ["Hello there.", "How are you?"] | `tests/pipeline/test_chunker.py` | planned |
| TC-BE-011 | TR-041 | Given "We use e.g. Whisper. It works.", then "e.g." does not split | same | planned |
| TC-BE-012 | TR-041 | Given "Latency is 3.5 seconds. Fine.", then the decimal does not split | same | planned |
| TC-BE-013 | TR-042 | Given a 90-char buffer with a comma at position 70 and no terminator, then it splits at the comma | same | planned |
| TC-BE-014 | TR-043 | Given 250 chars with no punctuation, then it splits at the last whitespace before 200 | same | planned |
| TC-BE-015 | TR-044 | Given "**Bold** and `code`", then the emitted segment has no markdown symbols | same | planned |
| TC-BE-016 | TR-040 | Given a partial trailing sentence, when `flush()` is called, then it is returned once and the buffer is empty | same | planned |
| TC-BE-017 | TR-045 | Given three segments, then their ids are 0, 1, 2 | same | planned |
| TC-BE-018 | TR-040 | Property (hypothesis): for any text, `"".join(segments)` equals the input with whitespace normalised and markdown stripped | same | planned |

### ConversationHistory

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-BE-020 | F7 / TR-051 | Given an assistant turn with sentences [s0, s1, s2] in progress, when truncated at 1, then content is "s0 s1 [interrupted by user]" | `tests/pipeline/test_history.py` | planned |
| TC-BE-021 | TR-051 | When truncated at `None`, then content is "[interrupted by user before speaking]" | same | planned |
| TC-BE-022 | TR-051 | Given a turn with an applied `go_to_slide` tool call, when truncated, then the tool call and result messages are retained | same | planned |
| TC-BE-023 | TR-052 | Given 25 user/assistant pairs, then `to_provider_messages()` contains the system message and the latest 20 pairs | same | planned |
| TC-BE-024 | TR-052 | Given the oldest pair has tool messages, when capped, then its tool messages are dropped too (no orphan `tool` role) | same | planned |
| TC-BE-025 | TR-053 | When `add_system_note("...")` is called, then a `system` message is appended after the last message | same | planned |
| TC-BE-026 | TR-054 | Then serialised messages contain no `sentences` key | same | planned |

### SlideController

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-BE-030 | F5 / TR-061 | Given `go_to_slide(4, "r")`, then action index 4 and current_slide becomes 4 | `tests/pipeline/test_slides.py` | planned |
| TC-BE-031 | TR-061 | Given `go_to_slide(9, ...)` on a 6-slide deck, then no action, a tool error string is returned, current_slide unchanged | same | planned |
| TC-BE-032 | TR-061 | Given `highlight_bullet(7)` on a slide with 4 bullets, then a tool error string | same | planned |
| TC-BE-033 | TR-061 | Given an unknown tool name, then a tool error string and no exception | same | planned |
| TC-BE-034 | TR-062 | Given answer text about "latency and milliseconds" while on slide 1, then fallback returns slide 2 with source fallback | same | planned |
| TC-BE-035 | TR-062 | Given answer text that mentions two slides equally, then fallback returns None (tie rule) | same | planned |
| TC-BE-036 | TR-062 | Given answer text about the current slide, then fallback returns None | same | planned |
| TC-BE-037 | TR-063 | Given cursor=2, when `on_user_navigation(5)`, then current_slide=5, cursor still 2, note mentions slide 5 title | same | planned |
| TC-BE-038 | TR-064 | Given mode=present and cursor=6 (last), when `advance_cursor()`, then cursor stays 6 and returns False | same | planned |
| TC-BE-039 | TR-032 | Given two `go_to_slide` calls in one turn (3 then 5), then current_slide is 5 and two actions were emitted | same | planned |

### Session state machine and turn pipeline (fake providers)

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-BE-040 | F2 / TR-020 | Given `session.start`, then `session.ready` and `state listening` are sent, in that order | `tests/test_session.py` | planned |
| TC-BE-041 | F13 / TR-020 | Given `text.input`, then states go THINKING → SPEAKING and a `transcript.user` is emitted with the text | same | planned |
| TC-BE-042 | F5 | Given FakeLLM scripted to call `go_to_slide(4)`, then `tool.call{source: llm}` and `slide.goto{index: 4}` are emitted before any audio frame | same | planned |
| TC-BE-043 | F5 / TR-062 | Given FakeLLM returns text about latency and no tool call, then `tool.call{source: fallback}` and `slide.goto{2}` are emitted after the text | same | planned |
| TC-BE-044 | F6 / TR-033 | Given FakeLLM streams two sentences, then `transcript.agent{0}` precedes audio frames with sentence_id 0, and same for 1 | same | planned |
| TC-BE-045 | TR-141 | Then every server binary frame has an 8-byte header and payload ≤ 4,800 bytes | same | planned |
| TC-BE-046 | F7 / TR-022/023 | Given a turn in SPEAKING, when `interrupt{last_completed: 0}` arrives, then the task is cancelled within 50 ms, `agent.cancelled{0}` is emitted, state is HEARING, history content ends with "[interrupted by user]" | same | planned |
| TC-BE-047 | TR-024 | Given state LISTENING, when `interrupt` arrives, then nothing is emitted and state unchanged | same | planned |
| TC-BE-048 | TR-024 | Given two `interrupt` messages 100 ms apart, then exactly one `agent.cancelled` | same | planned |
| TC-BE-049 | TR-023 | Given state THINKING (no audio yet), when `speech.start` arrives, then the turn is cancelled and truncation uses `None` | same | planned |
| TC-BE-050 | TR-021 | Given a cancelled turn n and a new turn n+1, then no message with `turn_id: n` is sent after `agent.cancelled` | same | planned |
| TC-BE-051 | TR-025 | Given FakeLLM that never finishes, then after 20 s `error{turn_timeout}` and state LISTENING | same (uses fake clock) | planned |
| TC-BE-052 | TR-170 | Given FakeSTT raising `ProviderError`, then `error{stt_failed, recoverable: true}` and state LISTENING | same | planned |
| TC-BE-053 | TR-172 | Given FakeSTT returning "", then no `transcript.user`, no LLM call, state LISTENING | same | planned |
| TC-BE-054 | F4 | Given FakeSTT returning "Thank you." (filler denylist), then the turn is dropped | same | planned |
| TC-BE-055 | TR-140 | Given a binary frame not preceded by `speech.end`, then `error{unexpected_binary}` | same | planned |
| TC-BE-056 | TR-182 | Given a 3 MB binary frame, then the socket closes with code 1009 | same | planned |
| TC-BE-057 | F9 / TR-063 | Given `slide.changed{4, user}`, then history gets a system note and the next prompt reports current_slide 4 | same | planned |
| TC-BE-058 | TR-026 | Given a turn in progress, when the socket disconnects, then the task is cancelled and the session removed from the manager | same | planned |
| TC-BE-059 | F8 | Given `control{start_presentation}`, then mode=present, the agent turn starts with cursor 1 and a `go_to_slide(1)` from the fake script advances the cursor | same | planned |
| TC-BE-060 | TR-173 | Given FakeTTS failing on sentence 1 of 3, then sentences 0 and 2 are sent and one ERROR log is recorded | same | planned |

### Providers (contract, recorded fixtures)

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-BE-070 | TR-082 | Given a recorded Groq SSE stream with a streamed tool call in argument fragments, when parsed, then one `ToolCallDelta` with parsed JSON args | `tests/providers/test_groq_llm.py` | planned |
| TC-BE-071 | TR-082 | Given a recorded SSE stream with text then `[DONE]`, then TokenDeltas followed by `LLMDone{stop}` | same | planned |
| TC-BE-072 | TR-082 / TR-085 | Given an HTTP 429 with `retry-after: 3`, then `ProviderError(retryable=True)` carrying 3 | same | planned |
| TC-BE-073 | TR-031 | Given a stream in progress, when the consuming task is cancelled, then the httpx response is closed (mock asserts `aclose`) | same | planned |
| TC-BE-074 | TR-081 | Given a 1 s 16 kHz PCM buffer, when wrapped, then a valid WAV header with sample rate 16,000, 1 channel, 16-bit | `tests/providers/test_groq_stt.py` | planned |
| TC-BE-075 | TR-083 | Given KokoroTTS (integration-lite, model present), when synthesising "Ready.", then ≥ 1 chunk, each ≤ 4,800 bytes, total duration 0.3–1.5 s | `tests/providers/test_kokoro_tts.py` (skipped if weights absent) | planned |
| TC-BE-076 | TR-080 | Given `STT_PROVIDER=bogus`, when building providers, then startup fails with a message listing valid values | `tests/providers/test_registry.py` | planned |
| TC-BE-077 | TR-176 | Given `LLM_PROVIDER=groq` and no `GROQ_API_KEY`, then startup fails naming `.env.example` | same | planned |

### Protocol

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-BE-080 | TR-142 | Given `{"type": "speech.end"}` without `duration_ms`, then validation fails and `error{bad_message}` | `tests/test_protocol.py` | planned |
| TC-BE-081 | TR-142 | Given `text.input` with 501 chars, then `bad_message` | same | planned |
| TC-BE-082 | §6.1 | Given sentence_id 7, seq 3, payload b"..", when framed, then bytes start with `07 00 00 00 03 00 00 00` | same | planned |
| TC-PAR-001 | TR-143 | Then the set of `type` literals in `protocol.py` equals the set in `protocol.ts` | `tests/test_protocol_parity.py` | planned |

---

## Backend integration (require `GROQ_API_KEY`, `-m integration`)

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-INT-001 | F4 | Given `fixtures/audio/how_do_you_handle_interruptions.wav`, when transcribed, then the text contains "interrupt" | `tests/integration/test_groq_stt_live.py` | planned |
| TC-INT-002 | F4 | Given `fixtures/audio/silence_2s.wav`, then transcript is empty or in the denylist | same | planned |
| TC-INT-003 | F5 | Given the real LLM and "how do you handle interruptions?" on slide 1, then a `go_to_slide(4)` tool call is emitted | `tests/integration/test_routing_live.py` | planned |
| TC-INT-004 | F5 | Given "what's the weather in London?", then no tool call and the answer is ≤ 2 sentences | same | planned |
| TC-INT-005 | F6 | Given the real pipeline with text input, then `first_audio` (server-side proxy: first audio frame sent) ≤ 1.5 s | `tests/integration/test_pipeline_live.py` | planned |

---

## Frontend unit (vitest)

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-FE-001 | F1 | Given the deck, when rendering `SlideDeck` at index 3, then slide 3 title is visible and dot 3 is active | `src/components/SlideDeck.test.tsx` | planned |
| TC-FE-002 | F1 / TR-133 | Given `slide.goto{index: 42}`, then the store clamps to 6 | `src/store.test.ts` | planned |
| TC-FE-003 | F1 | When ArrowRight is pressed with a live session, then `slide.changed{source: user}` is sent | `src/session/useSession.test.tsx` | planned |
| TC-FE-010 | F6 / TR-121 | Given frames arriving every 100 ms, when scheduled on `FakeAudioContext`, then each `start(when)` equals the previous end time (no gaps, no overlap) | `src/audio/playback.test.ts` | planned |
| TC-FE-011 | TR-121 | Given a frame arriving 150 ms late, then it is scheduled at `currentTime + 0.02` and a silence gap is recorded, not an overlap | same | planned |
| TC-FE-012 | F7 / TR-122 | Given 5 scheduled sources, when `flush()`, then every source's `stop` is called and the queue is empty | same | planned |
| TC-FE-013 | TR-123 | Given sentence 0 frames then the first frame of sentence 1, then `onSentenceComplete(0)` fires exactly once | same | planned |
| TC-FE-014 | TR-123 | Given the final sentence and then `metrics`, then `onSentenceComplete(last)` fires | same | planned |
| TC-FE-020 | F3 / TR-112 | Given `isPlaying=true` and 2 consecutive positive VAD frames, then no onset; on the 3rd, onset | `src/audio/vad.test.ts` | planned |
| TC-FE-021 | TR-113 | Given onset while playing, then `flush()` is called before `interrupt` is sent, and `interrupt.last_completed_sentence_id` equals the queue's last completed id | same | planned |
| TC-FE-022 | TR-114 | Given a 150 ms utterance, then no `speech.end` is sent; if an interrupt was sent, `interrupt.cancel` follows | same | planned |
| TC-FE-023 | TR-114 | Given onset then end, then the emitted utterance length equals pre-pad + speech within one frame | same | planned |
| TC-FE-030 | §6.1 | Given a binary frame with header (7, 3), when decoded, then `{sentenceId: 7, seq: 3, pcm}` | `src/protocol.test.ts` | planned |
| TC-FE-031 | TR-131 | Given store turnId 5 and an incoming `transcript.agent{turn_id: 4}`, then it is ignored | `src/store.test.ts` | planned |
| TC-FE-032 | F10 / TR-132 | Given `agent.cancelled{truncated_at: 1}` after sentences 0–3 were logged, then entries 2 and 3 render struck-through | `src/components/EventLog.test.tsx` | planned |
| TC-FE-033 | F12 | Given three metrics messages, then the HUD shows the last value and the median | `src/components/LatencyHUD.test.tsx` | planned |
| TC-FE-034 | TR-175 | Given an abnormal close (1006), then exactly one reconnect attempt with a new `session.start` | `src/session/client.test.ts` | planned |
| TC-FE-035 | TR-103 | Given five start/stop cycles with a fake `MediaStream`, then every track's `stop()` was called and all contexts closed | `src/audio/capture.test.ts` | planned |

---

## End-to-end (Playwright, fake-provider backend)

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-E2E-001 | PRD §13 | The full walkthrough scenario, steps 1–8, using text input and synthetic VAD events; asserts slide indices, log chips, and that audio is flushed on interrupt | `frontend/e2e/walkthrough.spec.ts` | planned |
| TC-E2E-002 | F2 | Given the backend is down, when Start is clicked, then the error toast with a retry button is shown; when the backend comes up and retry is clicked, the session connects | `frontend/e2e/connection.spec.ts` | planned |
| TC-E2E-003 | F13 | Given mic permission denied (Playwright permission), then the text input still produces a voice answer and a slide change | `frontend/e2e/fallback.spec.ts` | planned |
| TC-E2E-004 | F9 | Given manual navigation to slide 6 then text "explain this", then the agent answers without a `slide.goto` chip | `frontend/e2e/sync.spec.ts` | planned |

---

## Manual checklist (before each release)

| ID | Check | Result (v0.1.0) |
|---|---|---|
| TC-MAN-001 | Headphones, quiet room: 10 consecutive utterances detected as exactly one turn each; no clipped first words | — |
| TC-MAN-002 | Laptop speakers at normal volume: agent speaks a full slide without self-interruption | — |
| TC-MAN-003 | Interrupt 10 times mid-sentence: audio stops ≤ 150 ms every time (HUD), agent never repeats a completed sentence | — |
| TC-MAN-004 | Present mode runs all six slides unattended | — |
| TC-MAN-005 | Interrupt on slide 3 with a slide-5 question, then "continue": returns to slide 3 and resumes | — |
| TC-MAN-006 | Start/End session five times: no console errors, no orphan audio, mic indicator off after End | — |
| TC-MAN-007 | Fresh clone on a second machine: README quick start works in ≤ 5 commands | — |
| TC-MAN-008 | Safari: session starts, audio plays, VAD detects speech (best-effort; document failures) | — |
