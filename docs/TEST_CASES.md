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

## Phase 0 — scaffolding (implemented 2026-09-10)

Settings, health probe, and harness cases added when the scaffold landed. Several of these are
parametrised, so 18 test functions expand to 52 collected backend cases.

### Configuration (`backend/tests/test_config.py`)

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-BE-090 | TR-014 | Given no environment and no dotenv, when Settings is built, then providers default to groq / groq / kokoro and no API key is set | `tests/test_config.py::test_provider_defaults_are_the_declared_values` | passing |
| TC-BE-091 | TR-014 | Given the same, then every non-provider default (models, ports, tuning, pipeline limits) equals its declaration | `::test_server_generation_and_pipeline_defaults_are_the_declared_values` | passing |
| TC-BE-092 | TR-014 | Given `log_level` in any case, when validated, then it is stored upper-case | `::test_log_level_is_normalised_to_upper_case` | passing |
| TC-BE-093 | TR-014 | Given a level name the logging module does not define, then validation fails | `::test_unknown_log_level_is_rejected` | passing |
| TC-BE-094 | TR-180 | Given a configured `groq_api_key`, when `masked_dump()` runs, then the value is `"***"` and never the clear text | `::test_masked_dump_replaces_a_configured_secret_with_stars` | passing |
| TC-BE-099 | TR-014 | Given values exactly at the inclusive ends of every declared range, then they validate | `::test_boundary_values_are_accepted` | passing |
| TC-BE-100 | TR-180 | Given no `groq_api_key`, then `masked_dump()` reports `None`, not `"***"` | `::test_masked_dump_reports_none_when_no_secret_is_configured` | passing |
| TC-BE-101 | TR-180 | Given a distinctive fake secret, then neither `repr()` nor `str()` of Settings contains it | `::test_repr_and_str_never_expose_the_raw_secret` | passing |
| TC-BE-102 | TR-014 | Given out-of-range numerics (port 0 / 70000, temperature 5.0, max_tokens 0), then each raises | `::test_out_of_range_values_are_rejected` | passing |
| TC-BE-103 | TR-014 | Given repeated `get_settings()` calls, then the same object is returned, and `cache_clear()` starts a new one | `::test_get_settings_returns_the_same_cached_instance` | passing |
| TC-BE-104 | TR-014 | Given the module constants, then `REPO_ROOT` is the directory holding `docs/` and `CLAUDE.md`, proving the `parents[N]` arithmetic | `::test_repo_root_is_the_directory_holding_docs_and_claude_md` | passing |
| TC-BE-105 | TR-014 | Given environment variables in either case, then they override defaults | `::test_environment_variables_override_defaults_case_insensitively` | passing |
| TC-BE-106 | TR-080 | Given a provider name outside the declared literals, then validation fails | `::test_unknown_provider_names_are_rejected` | passing |
| TC-BE-107 | TR-014 | Given the suite's dotenv redirect, then every Settings instantiation reads the isolated file and never the developer's real `.env` | `::test_settings_read_the_isolated_env_file_not_the_repo_root_one` | passing |

### Health probe (`backend/tests/test_health.py`)

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-BE-095 | TR-192, §4.10 | Given a running app, when `GET /api/health`, then 200 with exactly the four documented keys and a non-empty version | `tests/test_health.py::test_health_returns_ok_with_the_documented_key_set` | passing |
| TC-BE-096 | TR-192 | Then the `providers` block mirrors the configured settings, with no extra keys | `::test_health_reports_the_configured_providers` | passing |
| TC-BE-097 | TR-013, TR-192 | Then `tts_warm` is a boolean and is false before any warm-up has run | `::test_health_reports_tts_warm_as_false_before_warm_up` | passing |
| TC-BE-098 | TR-192 | Given reconfigured providers, then the probe reports the new selection | `::test_health_reflects_overridden_provider_configuration` | passing |

### Structured logging (`backend/tests/test_logging_setup.py`) — added closing a review gap

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-BE-110 | TR-190 | Given `log_json=True`, when a line is logged, then it parses as one JSON object carrying event, level, logger, and an ISO timestamp | `::test_json_mode_emits_one_parsable_object_per_call` | passing |
| TC-BE-111 | TR-190 | Given `log_json=False`, then output is a human console line that is not JSON | `::test_console_mode_renders_a_human_line_that_is_not_json` | passing |
| TC-BE-112 | TR-190 | Given a standard-library `uvicorn.access` record, then it renders through the same handler and formatter as a structlog call | `::test_uvicorn_records_are_rendered_by_the_same_handler_as_structlog` | passing |
| TC-BE-113 | TR-190 | Given `configure_logging` called twice, then exactly one named handler remains on the root logger | `::test_configuring_twice_leaves_exactly_one_named_handler` | passing |
| TC-BE-114 | TR-190 | Given a logger already in use, when reconfigured from console to JSON, then its output format actually changes | `::test_reconfiguring_from_console_to_json_changes_an_existing_logger` | passing |
| TC-BE-115 | TR-190 | Then every bridged logger ends with no handlers of its own and propagates to root | `::test_bridged_logger_ends_with_no_handlers_and_propagating` | passing |
| TC-BE-116 | TR-190 | Then the configured level is applied to the root and to every bridged logger | `::test_configured_level_is_applied_to_root_and_bridged_loggers` | passing |
| TC-BE-117 | TR-190 | Given a call below the threshold, then nothing is emitted; at the threshold it is | `::test_records_below_the_threshold_are_suppressed` | passing |
| TC-BE-118 | TR-190 | Given keys bound to a logger, then they appear in every line it renders | `::test_get_logger_supports_bound_context_that_reaches_the_output` | passing |
| TC-BE-119 | TR-190 | Given context bound via contextvars, then it reaches both structlog-native and bridged records | `::test_context_variables_are_merged_into_native_and_bridged_records` | passing |

### Error hierarchy (`backend/tests/test_errors.py`) — added closing a review gap

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-BE-120 | TR-085, TR-170 | Then every concrete error subclasses `AppError`, the sole condition under which the registered handler fires | `::test_every_application_error_descends_from_app_error` | passing |
| TC-BE-121 | TR-085 | Then the hierarchy is flat, siblings are unrelated, and no error exists outside the documented set | `::test_the_hierarchy_is_flat_and_the_catalogue_above_is_exhaustive` | passing |
| TC-BE-122 | TR-085, TR-171 | Then `ProviderError` keeps provider, message, retryable and retry_after, and prefixes the provider in `str()` | `::test_provider_error_carries_its_fields_and_prefixes_the_provider_name` | passing |
| TC-BE-123 | TR-085 | Given the optional flags omitted, then retryable is False and retry_after is None | `::test_provider_error_defaults_to_not_retryable_with_no_retry_after` | passing |
| TC-BE-124 | TR-142 | Then `ProtocolError` keeps its code and stringifies to the message | `::test_protocol_error_carries_its_code_and_stringifies_to_the_message` | passing |
| TC-BE-125 | TR-170 | Then any application error raised is caught by `except AppError` | `::test_every_error_is_raisable_and_caught_as_app_error` | passing |
| TC-BE-126 | TR-085 | Given a wrapped third-party failure, then the original stays reachable via `__cause__` | `::test_raise_from_preserves_the_original_exception_as_cause` | passing |

### Startup and fail-fast (`backend/tests/test_startup.py`) — added closing a review gap

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-BE-130 | TR-013 | Given the client entered as a context manager, then the lifespan runs and builds an `AppState` holding the settings | `::test_lifespan_stores_an_app_state_holding_the_settings` | passing |
| TC-BE-131 | TR-192 | Then the health probe answers from the state the lifespan built, not a fallback | `::test_health_answers_from_the_lifespan_state_inside_the_context` | passing |
| TC-BE-132 | **TR-180** | Given a distinctive key in the environment, when the app starts, then the startup log contains `"***"` and never the raw secret | `::test_startup_logs_the_settings_with_the_secret_masked` | passing |
| TC-BE-133 | TR-176 | Given an invalid field value, then `_load_settings` raises `ConfigError` with the original as `__cause__` | `::test_load_settings_wraps_a_validation_error_in_config_error` | passing |
| TC-BE-134 | TR-176 | Given a comma-separated `CORS_ORIGINS`, then the `SettingsError` from the settings source is wrapped in `ConfigError` too (regression, fixed 2026-09-10) | `::test_load_settings_wraps_a_settings_source_error_in_config_error` | passing |
| TC-BE-135 | TR-176 | Given a blank `GROQ_API_KEY`, then it normalises to `None`, not a set-but-empty secret (regression, fixed 2026-09-10) | `::test_a_blank_groq_api_key_normalises_to_none` | passing |
| TC-BE-136 | TR-176 | Given the file `cp .env.example .env` produces, then the app boots with no secret configured | `::test_a_copied_env_example_leaves_no_secret_and_still_boots` | passing |
| TC-BE-137 | TR-170 | Given a route raising `ProviderError`, then the response is 500 with the error class name and message | `::test_an_app_error_becomes_a_500_json_body_naming_the_error` | passing |

### Frontend harness (`frontend/src/smoke.test.ts`)

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-FE-090 | — | Given the vitest harness, then jsdom, the setup file, and the jest-dom matchers are all live (asserts the environment, not arithmetic) | `src/smoke.test.ts::TC-FE-090` | passing |
| TC-FE-091 | F1 | Given a mocked healthy backend, when App renders, then the deck title heading shows and the status reads ok | `src/smoke.test.ts::TC-FE-091` | passing |
| TC-FE-092 | F2, TR-175 | Given a health probe that rejects, then the backend is reported unreachable rather than throwing | `src/smoke.test.ts::TC-FE-092` | passing |
| TC-FE-093 | TR-212 | Given a rendered App, then the probe requests exactly `/api/health` with an abort signal. Mutation-tested: changing the URL fails this case | `src/smoke.test.ts::TC-FE-093` | passing |
| TC-FE-094 | F2 | Given a 503 response, then the status reads unreachable (exercises the `response.ok` branch, which no test previously reached) | `src/smoke.test.ts::TC-FE-094` | passing |
| TC-FE-095 | TR-103 | Given a probe that never settles, when App unmounts, then the request is aborted and no state update leaks | `src/smoke.test.ts::TC-FE-095` | passing |

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
