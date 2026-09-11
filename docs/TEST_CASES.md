# Test Case Catalogue

Living document. Every behavioural change adds or updates an entry here **in the same commit**. IDs are stable; never reuse a retired ID. Status values: `planned`, `implemented`, `passing`, `partial`, `skipped`, `failing`, `retired`.

Columns: **ID** · **Feature / TR** (PRD feature and TRD requirement it verifies) · **Layer** · **Given / When / Then** · **Test location** · **Status**.

Naming: `TC-<layer>-<nnn>` where layer ∈ `BE` (backend unit/contract), `INT` (backend integration, needs key), `FE` (frontend unit), `PAR` (protocol parity), `E2E` (Playwright), `MAN` (manual checklist). A lower-case letter suffix (`TC-BE-031a`) is a sub-case added inside an existing behaviour; it is an ID in its own right and obeys the same rules.

Status beyond the obvious:

- `partial` — the test exists and passes, but it covers only part of the row. The row names what is still missing today, and the test's own docstring usually says the same thing.
- `skipped` — the test exists but is not run, because the behaviour has no enforcement point yet. No row carries this status now: the backend suite skips nothing.
- `planned` — no test exists. The row names the milestone that will write it.
- `retired` — superseded. Kept so the ID is never reused.

**Reconciled with the tree on 2026-09-11**, after M1 (text loop), M2 (audio out), M3 (audio in and barge-in) and the walkthrough and push-to-talk parts of M4: 542 backend cases across 21 files, all passing with none skipped, plus six the default run deselects because they spend a real API key or load the real synthesiser, and 163 frontend cases across 17 files, all passing, plus five Playwright cases in `frontend/e2e/` run by `make test-e2e`. Locations are real paths; `tests/` is relative to `backend/`, `src/` and `e2e/` to `frontend/`. Where one row is carried by several tests, they are listed together; where one test carries several rows, it is named by each of them.

Every backend ID is claimed by exactly one test; four frontend IDs are still claimed twice, and §1 of the reconciliation items below names them. The collisions created by parallel authoring were renumbered on 2026-09-11: `test_history.py` moved to the 220 block and `test_turn.py` to 232-237, later joined by 242-243. IDs are never reused.

---

## Backend unit and contract

### Deck and prompt

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-BE-001 | F1 / TR-150 | Given the default deck JSON, when loaded, then it validates with 6 slides, contiguous indices, unique aliases | `tests/test_decks.py::test_the_shipped_deck_loads_with_six_slides_and_unique_aliases` | passing |
| TC-BE-002 | TR-150 | Given a deck with duplicate aliases across slides, when loaded, then `DeckError` names both slides | `::test_a_duplicate_alias_across_slides_is_a_deck_error_naming_both_slides` | passing |
| TC-BE-003 | TR-150 | Given a deck with 4 slides, when loaded, then validation fails (min 5) | `::test_a_deck_with_four_slides_is_rejected_by_the_minimum` | passing |
| TC-BE-140 | F1 / TR-150 | Given the shipped deck, then every slide keeps speakable notes, glanceable bullets, and the aliases the PRD names | `::test_the_shipped_deck_meets_the_authoring_contract` | passing |
| TC-BE-140a | F1 / TR-071 | Then no shipped slide's notes are truncated on the way into the prompt — the authored copy fits the budget | `::test_every_authored_note_reaches_the_prompt_whole` | passing |
| TC-BE-141 | TR-150 | Given a deck, then `slide()` indexes from 1 and raises outside the deck, and `last_index` names the final slide | `::test_slide_lookup_is_one_based_and_last_index_names_the_final_slide` | passing |
| TC-BE-142 | TR-071 | Given notes at or under the prompt budget, then they pass through unchanged; longer notes are elided once | `::test_prompt_notes_truncate_only_once_past_the_prompt_budget` | passing |
| TC-BE-143 | TR-150 | Given an unreadable, unparsable, or schema-invalid deck file, then the `DeckError` names the file | `::test_a_malformed_deck_file_raises_a_deck_error_naming_the_file` | passing |
| TC-BE-144 | TR-150 | Given a deck failing many validators, then the `DeckError` quotes the broken field path and elides a long tail | `::test_a_validation_failure_names_the_field_path_and_caps_the_problem_list` | passing |
| TC-BE-145 | TR-150 | Given two files declaring the same deck id, when loaded, then it fails naming both files | `::test_two_files_declaring_the_same_deck_id_name_both_files` | passing |
| TC-BE-146 | TR-151 | Given an id the repository does not hold, when `get` is called, then `DeckError` lists the ids that do exist | `::test_getting_an_unknown_deck_id_raises_and_lists_what_is_known` | passing |
| TC-BE-147 | TR-151 | Then `list_decks` returns id/title/slide_count ordered by id, and `__len__` agrees with it | `::test_listing_summarises_each_deck_and_length_agrees` | passing |
| TC-BE-148 | TR-151 | Given a deck built at runtime, when `register`ed, then it is served; registering the same id twice raises | `::test_registering_a_runtime_deck_adds_it_and_a_repeated_id_is_refused` | passing |
| TC-BE-149 | TR-151 | Given the repository is constructed, when the source file is deleted, then later lookups are unaffected (read once) | `::test_decks_are_read_once_at_construction_and_never_re_read` | passing |
| TC-BE-004 | TR-070/071 | Given a deck whose notes contain `{braces}`, when the prompt is built, then no exception and braces are preserved | `tests/test_prompt.py::test_braces_in_deck_copy_render_literally` | passing |
| TC-BE-005 | TR-071 | Given notes authored up to the deck's own limit, when the prompt is built, then they reach it truncated. Written against the constants, not a literal: the budget moved when the prompt stopped carrying all six slides' notes | `::test_notes_longer_than_the_prompt_budget_are_truncated` | passing |
| TC-BE-006 | TR-070 | Given current_slide=3, cursor=2, mode=present, when built, then all three appear in the system prompt and no placeholder is left unfilled (parametrised over the enum and its bare string) | `::test_the_snapshot_position_appears_in_the_prompt` | passing |
| TC-BE-007 | TR-070 | Given the deck, when the prompt is built, then every slide's title and bullets reach the model as compact JSON | `::test_the_prompt_embeds_every_slides_title_and_bullets` | passing |
| TC-BE-008 | TR-070 | Given a history, when `build` runs, then the system message leads and the history follows untouched | `::test_the_system_message_leads_and_history_follows_in_order` | passing |
| TC-BE-009 | TR-070 | Given a foreign working directory, a missing template, and odd deck copy, then loading fails loudly only when the file is absent and rendering itself never raises | `::test_the_template_loads_from_the_package_and_renders_anything` | passing |
| TC-BE-160 | TR-070 / TR-086 | Then aliases never reach the model — they are server-side scoring input worth ~300 input tokens a request | `::test_aliases_are_not_sent_to_the_model` | passing |
| TC-BE-161 | TR-071 / TR-086 | Then only the current slide's notes are embedded, not all six (~1,570 tokens a request on the free tier) | `::test_only_the_current_slides_notes_are_embedded` | passing |

### SentenceChunker

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-BE-010 | F6 / TR-040 | Given tokens forming "Hello there. How are you?", when fed token by token, then segments are ["Hello there.", "How are you?"] | `tests/test_chunker.py::test_tokens_are_reassembled_into_whole_sentences` | passing |
| TC-BE-011 | TR-041 | Given "We use e.g. Whisper. It works.", then "e.g." does not split (six abbreviations parametrised); a trailing abbreviation waits for the next token | `::test_a_known_abbreviation_does_not_end_a_sentence`, `::test_an_abbreviation_at_the_end_of_the_buffer_waits_for_more_text` | passing |
| TC-BE-012 | TR-041 | Given "Latency is 3.5 seconds. Fine.", then the decimal does not split, including when the token boundary falls inside "3.5" | `::test_a_decimal_point_does_not_end_a_sentence`, `::test_a_decimal_split_across_two_tokens_still_does_not_end_a_sentence` | passing |
| TC-BE-013 | TR-042 | Given a buffer past the early-split minimum with a clause boundary, then it splits at the last boundary before the limit; below the limit a comma is a pause, not an end | `::test_a_long_buffer_splits_at_a_clause_boundary`, `::test_the_clause_split_uses_the_last_boundary_before_the_limit`, `::test_a_short_buffer_is_never_split_at_a_boundary` | passing |
| TC-BE-013a | TR-042 | Given a boundary too near the front of the buffer ("So, …"), then no segment is released — two characters is not worth synthesising | `::test_a_leading_connective_is_not_spoken_on_its_own` | passing |
| TC-BE-013b | TR-042 | Then the head floor is exact: one character short and the early split is skipped | `::test_the_early_split_needs_a_head_worth_speaking` | passing |
| TC-BE-013c | TR-042 / TR-086 | Then the floor does not become a way of never splitting early — a boundary that clears it still fires, which is what buys `first_audio_ms` | `::test_a_usable_boundary_still_releases_the_first_clause_early` | passing |
| TC-BE-014 | TR-043 | Given 250 chars with no punctuation, then it splits at the last whitespace before 200 | `::test_text_with_no_punctuation_splits_at_the_last_whitespace` | passing |
| TC-BE-014a | TR-043 | Given a boundary the head floor skipped, then the hard limit is still free to fire — a skipped boundary never wedges the chunker | `::test_an_unspeakable_boundary_does_not_wedge_the_chunker` | passing |
| TC-BE-015 | TR-044 | Given "**Bold** and `code`", then the emitted segment has no markdown symbols; link and image syntax are unwrapped too | `::test_markdown_is_stripped_before_a_segment_is_emitted` | passing |
| TC-BE-016 | TR-040 | Given a partial trailing sentence, when `flush()` is called, then it is returned once and the buffer is empty; an untouched chunker flushes to nothing | `::test_flush_returns_the_partial_sentence_once_and_empties_the_buffer`, `::test_flush_on_an_untouched_chunker_returns_nothing` | passing |
| TC-BE-017 | TR-045 | Given three segments, then their ids are 0, 1, 2 | `::test_segments_arrive_in_order_so_the_caller_can_number_them_from_zero` | passing |
| TC-BE-018 | TR-040 | Property (hypothesis): concatenated segments equal the speech-normalised input, whitespace aside, and any fragmentation gives the same segments as feeding the text whole | `::test_segments_preserve_every_non_whitespace_character_in_order`, `::test_chunking_does_not_depend_on_how_the_stream_was_fragmented` | passing |
| TC-BE-019 | TR-040 | Given empty, whitespace-only, pure-punctuation or pure-markdown input, then no empty or whitespace-only segment is ever emitted | `::test_an_empty_or_whitespace_only_segment_is_never_emitted` | passing |
| TC-BE-020 | TR-044 | Given curly quotes, en/em dashes and an ellipsis from a live run, then they reach the synthesiser as ASCII | `::test_typographic_punctuation_is_normalised_for_speech` | passing|
| TC-BE-020a | TR-044 | Given `snake_case`, then removing the symbol separates the words it sat between instead of welding them into one unpronounceable word | `::test_stripping_a_symbol_separates_the_words_it_sat_between` | passing |
| TC-BE-020b | TR-044 | Given a segment of zero-width characters — `gpt-oss-120b` ended a live turn with 221 of them (`docs/EVALS.md`) — then it is dropped, not spoken | `::test_invisible_characters_never_reach_the_synthesiser` | passing |

### ConversationHistory

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-BE-220 | F7 / TR-051 | Given an assistant turn with sentences [s0, s1, s2] in progress, when truncated at 1, then content is "s0 s1 [interrupted by user]" | `tests/test_history.py::test_truncating_mid_turn_keeps_only_the_sentences_that_were_heard` | passing|
| TC-BE-021 | TR-051 | When truncated at `None`, then content is "[interrupted by user before speaking]" | `::test_truncating_before_playback_started_records_that_nothing_was_said` | passing |
| TC-BE-022 | TR-051 | Given a turn with an applied `go_to_slide` tool call, when truncated, then the tool call and result messages are retained | `::test_truncation_retains_the_tool_call_and_result_of_the_cut_turn` | passing |
| TC-BE-023 | TR-052 | Given 25 user/assistant pairs, then `to_provider_messages()` contains the system message and the latest 20 pairs | `::test_capping_keeps_the_system_message_and_the_most_recent_pairs` | passing |
| TC-BE-024 | TR-052 | Given the oldest pair has tool messages, when capped, then its tool messages are dropped too (no orphan `tool` role) | `::test_capping_drops_the_tool_messages_of_a_dropped_pair` | passing |
| TC-BE-025 | TR-053 | When `add_system_note("...")` is called, then a `system` message is appended after the last message | `::test_a_system_note_is_appended_as_a_system_message_at_the_end` | passing |
| TC-BE-026 | TR-054 | Then serialised messages contain no `sentences` key (nor `turn_id`), while the bookkeeping survives internally | `::test_serialised_messages_never_carry_the_sentences_bookkeeping` | passing |
| TC-BE-027 | TR-051 | Given three recorded sentences, then `record_sentence` returns ids 0, 1, 2 and truncating at id 1 cuts there | `::test_recorded_sentences_are_numbered_from_zero_and_feed_truncation` | passing |
| TC-BE-028 | TR-024 / TR-051 | Given a truncate for a turn that is not current, then it returns False and changes nothing (a late interrupt is a race, not an error) | `::test_truncating_a_turn_that_is_not_current_changes_nothing` | passing |
| TC-BE-029 | TR-052 | Given 25 pairs each carrying tool messages, when capped, then every retained `tool` message is still paired with its announced call | `::test_capping_leaves_every_retained_tool_message_paired` | passing |
| TC-BE-200 | F7 / TR-051 | Given a turn the model has finished generating, when an interrupt lands before the next turn begins, then the finished answer is cut in place — the barge-in window outlives generation, and the completion cannot come back to undo the cut | `::test_a_finished_turn_can_still_be_truncated_until_the_next_one_begins` | passing |
| TC-BE-201 | TR-051 | Given the next turn has begun, then the previous turn is no longer truncatable — starting a turn is the only thing that closes the window | `::test_the_finished_turn_stops_being_truncatable_once_the_next_one_begins` | passing |
| TC-BE-202 | TR-025 / TR-051 | Given an end nobody asked for (watchdog or provider failure), then `last_recorded_sentence_id` is the cut — everything handed to TTS was heard, nothing after it was; with no turn in progress it is None | `::test_the_last_recorded_sentence_is_the_cut_for_an_end_nobody_asked_for`, `::test_no_turn_in_progress_has_no_last_recorded_sentence` | passing |

Depth cases for the same class. ⚠ All eleven cite IDs that the deck-repository block
(`tests/test_decks.py`, TC-BE-140–149) and the Groq-LLM block (`tests/test_groq_llm.py`,
TC-BE-150) already hold. They are catalogued here under the ID each docstring claims; the
renumbering belongs to whoever owns `tests/test_history.py`.

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-BE-221 | TR-050 | Given no assistant turn in progress, when `record_sentence` is called, then it raises `RuntimeError` rather than losing the sentence | `tests/test_history.py::test_recording_a_sentence_without_a_turn_is_a_programming_error` | passing|
| TC-BE-222 | F7 / TR-051 | Given a truncated turn, when the turn task finishes a moment later and calls `add_assistant`, then the unheard sentences are not restored | `::test_a_late_completion_cannot_restore_the_sentences_nobody_heard` | passing|
| TC-BE-223 | TR-023 / TR-051 | Given a VAD-triggered truncate at 0, when the client follows with a more precise id 1, then the same message is refined, not duplicated | `::test_a_second_more_precise_interrupt_refines_the_same_message` | passing|
| TC-BE-224 | TR-054 / TR-082 | Then a recorded tool call serialises in the OpenAI wire shape, with arguments as a JSON **string** | `::test_tool_calls_are_serialised_in_the_openai_wire_shape` | passing|
| TC-BE-225 | TR-051 | Given last_id past the end, at 0, with no sentences, or negative, then the content never ends in a dangling marker | `::test_truncation_boundaries_never_produce_a_dangling_marker` | passing|
| TC-BE-226 | TR-052 | Then the cap defaults to `Settings.max_history_turns`, is overridable, and refuses 0 | `::test_the_cap_comes_from_settings_and_must_be_at_least_one` | passing|
| TC-BE-227 | TR-054 | Then `to_provider_messages()` returns `Message` objects with the system prompt first | `::test_serialisation_returns_provider_messages_with_the_system_prompt_first` | passing|
| TC-BE-228 | TR-054 | Given the `messages` snapshot, when a caller mutates it, then the real history is unchanged | `::test_the_messages_snapshot_cannot_be_used_to_corrupt_history` | passing|
| TC-BE-229 | TR-050 | Given `add_assistant` with no explicit sentence list, then the turn falls back to the sentences it recorded and closes | `::test_a_completed_turn_falls_back_to_the_sentences_it_recorded` | passing|
| TC-BE-230 | TR-052/053 | Given a cap of one pair, then the pinned system prompt survives but a system note ages out with its pair | `::test_the_pinned_prompt_survives_capping_but_a_note_ages_out_with_its_pair` | passing|
| TC-BE-231 | TR-050 | Given a turn neither completed nor truncated, when the next turn begins, then the orphan is discarded | `::test_a_turn_left_unfinished_is_discarded_when_the_next_one_begins` | passing|

### SlideController

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-BE-030 | F5 / TR-061 | Given `go_to_slide(4, "r")`, then action index 4 and current_slide becomes 4 | `tests/test_slides.py::test_go_to_slide_moves_the_deck` | passing |
| TC-BE-030a | TR-060/064 | Then `snapshot()` reports exactly current_slide, presentation_cursor, mode and slide_count | `::test_snapshot_reports_the_prompt_facts` | passing |
| TC-BE-030b | TR-060 | Given a starting slide of 99 or 0, when constructed, then it clamps to 6 and 1 | `::test_construction_clamps_an_impossible_starting_slide` | passing |
| TC-BE-030c | TR-060 | Given two controllers over one deck, then moving one leaves the other where it was | `::test_controllers_over_one_deck_are_independent` | passing |
| TC-BE-031 | TR-061 | Given `go_to_slide(9, ...)` on a 6-slide deck, then no action, a tool error naming the real range, current_slide unchanged | `::test_go_to_slide_beyond_the_deck_is_refused` | passing |
| TC-BE-031a | TR-061 | Given `go_to_slide(0)`, then it is refused and the deck does not move | `::test_go_to_slide_below_the_deck_is_refused` | passing |
| TC-BE-031b | TR-061 | Given `go_to_slide(6)` on a 6-slide deck, then the last slide is accepted (off-by-one guard) | `::test_go_to_slide_accepts_the_last_slide` | passing |
| TC-BE-031c | TR-061 | Given `slide_index: "3"`, then the numeric string is coerced and applied | `::test_go_to_slide_coerces_a_numeric_string` | passing |
| TC-BE-031d | TR-061 | Given `slide_index` of None, True, 2.5, "third", "" or [3], then each is refused with a tool error | `::test_go_to_slide_refuses_a_non_integer_index` | passing |
| TC-BE-031e | TR-061 | Given a call with no `reason`, then one is supplied rather than an empty chip | `::test_go_to_slide_without_a_reason_supplies_one` | passing |
| TC-BE-031f | TR-061 | Given a 500-character `reason`, then it is truncated to `MAX_REASON_CHARS` | `::test_go_to_slide_truncates_a_rambling_reason` | passing |
| TC-BE-032 | TR-061 | Given `highlight_bullet(7)` on a slide with 4 bullets, then a tool error naming the range 0 to 3 | `::test_highlight_bullet_beyond_the_slide_is_refused` | passing |
| TC-BE-032a | TR-061 | Given `highlight_bullet(0)`, then the highlight is applied to the current slide and never navigates | `::test_highlight_bullet_within_the_slide_is_applied` | passing |
| TC-BE-032b | TR-061 | Given the deck moved to a shorter slide, then a bullet index valid on the old slide is refused on the new one | `::test_highlight_bullet_is_validated_against_the_current_slide` | passing |
| TC-BE-032c | TR-061 | Given `highlight_bullet(-1)`, then it is refused | `::test_highlight_bullet_refuses_a_negative_index` | passing |
| TC-BE-032d | TR-061 | Given `bullet_index` of None, True, 1.5, "second" or {}, then each is refused | `::test_highlight_bullet_refuses_a_non_integer_index` | passing |
| TC-BE-033 | TR-061 | Given an unknown tool name, then a tool error naming it and no exception | `::test_unknown_tool_is_refused_without_raising` | passing |
| TC-BE-033a | TR-061 | Given a refused call followed by a valid one, then `last_error` is cleared | `::test_a_valid_call_clears_the_previous_error` | passing |
| TC-BE-034 | TR-062 | Given answer text about "latency and milliseconds" while on slide 1, then fallback returns slide 2 with source fallback | `::test_fallback_routes_an_untooled_answer` | passing |
| TC-BE-035 | TR-062 | Given answer text that mentions two slides equally, then fallback returns None (tie rule) | `::test_fallback_declines_a_tie` | passing |
| TC-BE-035a | TR-062 | Given a one-point margin between the top two slides, then fallback declines | `::test_fallback_declines_a_one_point_margin` | passing |
| TC-BE-035b | TR-062 | Given a two-point margin, then fallback moves the deck | `::test_fallback_accepts_a_two_point_margin` | passing |
| TC-BE-035c | TR-062 | Given the current slide is the runner-up, then it still counts in the ranking, so a near miss does not jump | `::test_fallback_counts_the_current_slide_as_a_rival` | passing |
| TC-BE-036 | TR-062 | Given answer text about the current slide, then fallback returns None | `::test_fallback_declines_the_current_slide` | passing |
| TC-BE-036a | TR-062 | Given text that would win outright but names the slide already on screen, then fallback declines | `::test_fallback_declines_the_current_slide_even_when_it_wins_outright` | passing |
| TC-BE-036b | TR-062 | Given a score exactly `MIN_FALLBACK_SCORE`, then the fallback fires (inclusive threshold) | `::test_fallback_meets_the_threshold_exactly` | passing |
| TC-BE-036c | TR-062 | Given a score one point under the threshold and unopposed, then the fallback declines | `::test_fallback_declines_below_the_threshold` | passing |
| TC-BE-036d | TR-062 | Given an alias whose words appear scrambled, then it does not match; in order it does (phrases, not bags of words) | `::test_fallback_matches_aliases_as_phrases_not_loose_words` | passing |
| TC-BE-036e | TR-062 | Given an alias containing stopwords ("time to first token"), then the stopwords are kept inside the phrase | `::test_fallback_keeps_stopwords_inside_an_alias_phrase` | passing |
| TC-BE-036f | TR-062 | Given an answer of pure stopwords, then nothing scores | `::test_fallback_ignores_an_answer_of_pure_stopwords` | passing |
| TC-BE-036g | TR-062 | Given "", "   " or "!!! ... ???", then the fallback declines and the deck does not move | `::test_fallback_ignores_an_empty_answer` | passing |
| TC-BE-036h | TR-062 | Then the action's reason names the alias that matched, so the EventLog chip is explainable | `::test_fallback_reason_names_the_matched_alias` | passing |
| TC-BE-036i | TR-062 | Given a below-threshold phrase repeated six times, then repetition alone does not move the deck | `::test_repetition_alone_does_not_move_the_deck` | passing |
| TC-BE-036j | TR-062 | Given one bullet word each for three slides, then the weakest signal never clears the threshold | `::test_bullet_words_alone_never_clear_the_threshold` | passing |
| TC-BE-036k | TR-062 | Given words printed on the slide already on screen, then they are not evidence of another slide — scoring is asymmetric, so a slide can otherwise be dragged off itself | `::test_fallback_ignores_evidence_the_slide_on_screen_already_shows` | passing |
| TC-BE-036l | TR-062 | Given the same answer read from any other slide, then it still routes — the shield is scoped to the slide the audience is looking at | `::test_the_slide_on_screen_only_shields_itself` | passing |
| TC-BE-036m | F5 / TR-062 | Given the shipped deck and slide 2 described in slide 2's own words, then the deck stays on slide 2 | `::test_fallback_does_not_drag_the_shipped_deck_off_the_latency_slide` | passing |
| TC-BE-036n | F5 / TR-062 | Given the shipped deck and an answer about barge-in, then the fallback still moves it to slide 4 — the recovery path for a missed tool call must keep working | `::test_fallback_still_routes_the_shipped_deck_when_the_answer_is_elsewhere` | passing |
| TC-BE-037 | TR-063 | Given cursor=2, when `on_user_navigation(5)`, then current_slide=5, cursor still 2, note mentions slide 5 title | `::test_user_navigation_moves_the_slide_but_not_the_cursor` | passing |
| TC-BE-037a | TR-063 | Given `on_user_navigation(99)`, then the index clamps to the last slide and the note says so | `::test_user_navigation_clamps_an_impossible_index` | passing |
| TC-BE-038 | TR-064 | Given mode=present and cursor=6 (last), when `advance_cursor()`, then cursor stays 6 and returns False | `::test_advance_cursor_stops_at_the_end_of_the_deck` | passing |
| TC-BE-038a | TR-064 | Given mode=qa, then `advance_cursor()` is inert and returns False | `::test_advance_cursor_is_inert_in_qa_mode` | passing |
| TC-BE-038b | TR-064 | Given mode=present, when the cursor advances, then `current_slide` does not follow it | `::test_advance_cursor_leaves_the_current_slide_alone` | passing |
| TC-BE-039 | TR-032 | Given two `go_to_slide` calls in one turn (3 then 5), then current_slide is 5 and two actions were emitted | `::test_two_navigations_in_one_turn_both_apply` | passing |

### Session state machine and turn pipeline (fake providers)

`tests/test_session.py` drives the state machine over a real WebSocket with
`fastapi.testclient`; `tests/test_turn.py` drives `run_turn` directly. The fakes stand in for the
providers, so the wiring under test is real and only the network is not. Synthesised audio now
travels the same socket, so rows naming TTS are driven end to end. No test uploads an utterance,
so rows naming STT are still driven through `text.input`, which reaches the same code in
`run_turn`; those rows say so.

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-BE-040 | F2 / TR-020 | Given `session.start`, then `session.ready` and `state listening` are sent, in that order; anything before `session.start` is a `bad_message` and starts no turn | `tests/test_session.py::test_session_start_is_answered_with_ready_then_listening`, `::test_a_message_before_session_start_is_refused` | passing |
| TC-BE-041 | F13 / TR-020 | Given `text.input`, then states go THINKING → SPEAKING and a `transcript.user` is emitted with the text | `::test_a_typed_question_runs_a_turn_and_returns_to_listening` | passing |
| TC-BE-042 | F5 | Given FakeLLM scripted to call `go_to_slide(4)`, then `tool.call{source: llm}` and `slide.goto{index: 4}` are emitted before any audio frame | `::test_a_tool_call_moves_the_deck_before_the_answer_is_spoken` | passing |
| TC-BE-043 | F5 / TR-062 | Given FakeLLM returns text about latency and no tool call, then `tool.call{source: fallback}` and `slide.goto{2}` are emitted after the text | `::test_the_keyword_fallback_moves_the_deck_when_no_tool_was_called` | passing |
| TC-BE-044 | F6 / TR-033 | Given FakeLLM streams two sentences, then `transcript.agent{0}` precedes audio frames with sentence_id 0, and same for 1 | `::test_each_sentence_is_announced_in_order_with_its_own_id`, `::test_audio_frames_ship_alongside_the_transcript` | passing — including the ordering half: each `transcript.agent` is asserted to arrive before the first audio frame of the sentence it announces |
| TC-BE-045 | TR-141 | Then every server binary frame decodes through the 8-byte header and carries whole samples, at most 4,800 bytes of them | `::test_audio_frames_ship_alongside_the_transcript` | passing |
| TC-BE-046 | F7 / TR-022/023 | Given a turn in SPEAKING, when `interrupt{last_completed: 0}` arrives, then the task is cancelled, `agent.cancelled{0}` is emitted, state is HEARING, history ends with "[interrupted by user]" | `::test_an_interrupt_cancels_the_turn_and_truncates_history`, `::test_an_interrupt_announces_the_interrupted_state_before_hearing` | passing — the interrupt lands on a turn that is genuinely SPEAKING. The budget asserted is 500 ms rather than the 50 ms TR-022 asks for, so a loaded machine cannot fail the case; measured live it is 1.9 ms |
| TC-BE-047 | TR-024 | Given state LISTENING, when `interrupt` arrives, then nothing is emitted and state unchanged | `::test_an_interrupt_while_listening_does_nothing` | passing |
| TC-BE-048 | TR-024 | Given two `interrupt` messages 100 ms apart, then exactly one `agent.cancelled` | `::test_two_interrupts_in_quick_succession_cancel_the_turn_once` | passing |
| TC-BE-049 | TR-023 | Given state THINKING (no audio yet), when `speech.start` arrives, then the turn is cancelled and truncation uses `None`; outside a turn it is turn-taking, not barge-in | `::test_speech_onset_before_any_sentence_truncates_with_none`, `::test_speech_onset_while_listening_only_moves_to_hearing` | passing |
| TC-BE-050 | TR-021 | Given a cancelled turn n and a new turn n+1, then no message with `turn_id: n` follows its `agent.cancelled` — `state` excepted, since the transition into HEARING reports the turn being left | `::test_a_cancelled_turn_sends_nothing_further_under_its_own_turn_id` | passing |
| TC-BE-051 | TR-025 | Given FakeLLM that never finishes, then the watchdog fires with `error{turn_timeout}` and state LISTENING (timeout configured down to milliseconds rather than waiting 20 s) | `::test_a_turn_that_never_finishes_times_out_and_returns_to_listening` | passing |
| TC-BE-052 | TR-170 | Given FakeSTT raising `ProviderError`, then `error{stt_failed, recoverable: true}` and state LISTENING | `::test_a_provider_failure_is_reported_as_recoverable` | passing — injected at the model in `::test_a_provider_failure_is_reported_as_recoverable` and at the transcriber in `::test_a_transcriber_failure_is_reported_and_the_session_survives`, which between them cover both codes the handler maps |
| TC-BE-053 | TR-172 | Given FakeSTT returning "", then no `transcript.user`, no LLM call, state LISTENING | `::test_an_empty_or_filler_question_is_dropped_without_an_answer` | passing — typed in `::test_an_empty_or_filler_question_is_dropped_without_an_answer` and spoken in `::test_an_utterance_that_transcribes_to_nothing_is_dropped`, which is the branch in `handle_utterance` a typed question never reaches |
| TC-BE-054 | F4 | Given FakeSTT returning "Thank you." (filler denylist), then the turn is dropped | same test, `denylist` parameter | passing — also spoken, in `::test_a_filler_transcript_is_dropped_the_same_way` |
| TC-BE-055 | TR-140 | Given a binary frame not preceded by `speech.end`, then `error{unexpected_binary}` naming the message it must follow, and the session survives | `::test_a_binary_frame_without_speech_end_is_refused` | passing |
| TC-BE-056 | TR-182 | Given a binary frame past `max_utterance_bytes`, then the socket closes with code 1009 and the transcriber is never called | `::test_an_oversized_utterance_closes_the_socket` | passing |
| TC-BE-057 | F9 / TR-063 | Given `slide.changed{4, user}`, then history gets a system note and the next prompt reports current_slide 4 | `::test_manual_navigation_is_told_to_the_model` | passing |
| TC-BE-058 | TR-026 | Given a turn in progress, when the socket disconnects, then the task is cancelled and the session removed from the manager | `::test_disconnecting_mid_turn_cancels_the_task_and_releases_the_session` | passing |
| TC-BE-059 | F8 | Given `control{start_presentation}`, then mode=present and every slide is visited in order with its own notes spoken, and the model is never called | `::test_start_presentation_walks_the_whole_deck_without_the_model` | passing — this row absorbs the duplicate of the same ID that the walkthrough section carried; the opening model turn it used to describe was replaced by the walkthrough itself |
| TC-BE-060 | TR-173 | Given FakeTTS failing on sentence 1 of 3, then all three sentences are still announced, the surviving audio is sent, and no error reaches the client | `::test_a_failing_sentence_is_skipped_and_logged` | passing — the ERROR log the row originally asked for is not asserted; the test checks what the listener is left with |
| TC-BE-061 | F8 | Given a turn in progress, when `control{pause}` arrives, then the turn stops and the session returns to LISTENING without an `agent.cancelled` | `::test_pause_cancels_the_turn_and_returns_to_listening` | passing |
| TC-BE-062 | TR-021 | Given `playback.progress` for a turn that is not current, or for a session that is not speaking, then it is silently dropped | `::test_playback_progress_for_another_turn_is_ignored` | passing — progress driving the return to LISTENING is a separate behaviour, pinned by TC-BE-212 |
| TC-BE-063 | TR-151 | Given `session.start` naming a deck that does not exist, then the error is unrecoverable and the session ends | `::test_an_unknown_deck_is_refused_as_unrecoverable` | passing |
| TC-BE-064 | TR-024 | Given a VAD misfire, when `interrupt.cancel` arrives, then it is accepted and changes nothing | `::test_an_interrupt_cancel_after_a_misfire_does_nothing` | passing |
| TC-BE-065 | TR-142 | Given a text frame past `max_json_message_bytes`, then it is rejected on size before anything tries to parse it | `::test_an_oversized_text_frame_is_refused_before_parsing` | passing |
| TC-BE-066 | TR-026 | Then `SessionManager.remove` drops one session and `close_all` empties the registry | `::test_the_session_manager_releases_every_session` | passing |
| TC-BE-203 | TR-024 | Given an interrupt inside the previous turn's debounce window but aimed at a new turn, then it is honoured — the debounce belongs to a turn, not to the wall clock | `::test_a_barge_in_on_a_new_turn_is_honoured_inside_the_previous_window` | passing |
| TC-BE-204 | PRD §7 | Given an interrupt, then INTERRUPTED is announced as a state before HEARING, not skipped over | `::test_an_interrupt_announces_the_interrupted_state_before_hearing` | passing |
| TC-BE-205 | TR-023 | Given `speech.start` then a precise `interrupt`, then the refined truncation point still lands on the turn it names | `::test_a_follow_up_interrupt_refines_the_cut_of_the_turn_it_names` | passing |
| TC-BE-207 | TR-022 | Given a turn still running, when a new turn starts, then the old one is cancelled and awaited first | `::test_a_new_turn_cancels_the_one_still_running` | passing |
| TC-BE-208 | TR-051 | Given the watchdog fires mid-answer, then the sentences the room already heard are kept — the timeout truncates the answer, it does not delete it | `::test_a_timed_out_turn_keeps_the_sentences_the_room_already_heard` | passing |
| TC-BE-209 | TR-051 | Given a rate limit mid-answer, then what was already said is not unsaid | `::test_a_provider_failure_keeps_the_sentences_the_room_already_heard` | passing |
| TC-BE-210 | TR-025 | Given a bug inside the turn task, then it is reported and the session recovers rather than wedging the client in THINKING | `::test_an_unexpected_failure_is_reported_and_the_session_recovers` | passing |
| TC-BE-211 | TR-051 | Given `control{pause}` mid-answer, then the agent stops without unsaying what the room heard | `::test_pause_keeps_the_sentences_the_room_already_heard` | passing |
| TC-BE-212 | TR-020 | Given `playback.progress` for the last sentence of the current turn, then the turn ends — playback completion is client truth | `::test_playback_progress_ends_a_turn_whose_last_sentence_finished` | passing |
| TC-BE-213 | TR-131 | Given progress from an interrupted or unfinished turn, then nothing changes (parametrised over SPEAKING/THINKING/other states) | `::test_playback_progress_is_ignored_unless_the_current_turn_is_speaking` | passing |
| TC-BE-214 | TR-020 | Given the turn task is still running, then a sentence the client finished is not the end of the answer | `::test_playback_progress_while_the_turn_is_still_running_is_ignored` | passing |
| TC-BE-215 | TR-021/022 | Given a turn starting as another finishes, then the finishing turn is cancelled first and never outlives its own turn id | `::test_a_turn_starting_as_another_finishes_cancels_it_first` | passing |
| TC-BE-216 | TR-023 | Given an interrupt landing before the answer reached history, then `agent.cancelled` claims no cut, whatever the client reported | `::test_an_interrupt_before_the_answer_reached_history_claims_no_cut` | passing |
| TC-BE-217 | TR-024 | Given a stray interrupt naming a turn that was heard in full, then that turn is not rewritten — an interrupt names one turn, and only that turn | `::test_a_stray_interrupt_does_not_rewrite_a_turn_that_was_heard_in_full` | passing |
| TC-BE-170 | F4 | Given empty text, whitespace, and each of Whisper's silence artefacts, then `is_filler` is True; text that merely resembles one is still answered; a filler turn asks the model nothing and remembers nothing | `tests/test_turn.py::test_filler_is_recognised_whatever_its_spacing_or_case`, `::test_a_real_question_is_never_filler`, `::test_a_filler_turn_is_dropped_before_the_model_is_asked` | passing |
| TC-BE-171 | TR-050/051 | Given a turn mid-flight, then each sentence is already in history by the time it is sent, so truncating at any sentence is honest; a completed answer is recorded whole | `::test_each_sentence_is_in_history_by_the_time_it_is_sent`, `::test_a_completed_answer_is_recorded_whole` | passing |
| TC-BE-172 | TR-061 | Given a hallucinated slide index or a tool that does not exist, then the refusal is reported back to the model as a tool result and nothing moves | `::test_an_invalid_tool_call_is_reported_to_the_model_and_moves_nothing`, `::test_an_unknown_tool_is_refused_without_raising` | passing |
| TC-BE-173 | TR-032 | Given the model finishes with `tool_calls`, then a second request is made and still offers tools; a turn finishing with `stop` costs one request | `::test_a_tool_call_finish_triggers_a_second_request_that_still_offers_tools`, `::test_a_plain_answer_costs_a_single_request` | passing |
| TC-BE-174 | TR-062 | Given an answer that called no tool, the fallback routes it; once a tool has fired the fallback is never consulted; a local or thin answer moves nothing | `::test_the_fallback_routes_an_answer_that_called_no_tool`, `::test_the_fallback_is_not_consulted_once_a_tool_has_fired`, `::test_the_fallback_leaves_the_deck_alone_when_the_answer_is_local` | passing |
| TC-BE-175 | TR-032 | Given the model calls a tool again on the second request, then the turn still stops after two requests | `::test_the_turn_stops_after_two_requests_however_the_model_finishes` | passing |
| TC-BE-176 | TR-170/171 | Given a `ProviderError` from each stage, then each reaches the client under its own error code; a retryable failure carrying retry-after is reported as `rate_limited` | `::test_a_provider_failure_maps_to_its_error_code`, `::test_a_throttled_provider_is_reported_as_rate_limited` | passing |
| TC-BE-177 | TR-022 | Then `cancel_task` awaits the task it cancelled, so two turns never overlap; cancelling no task, or a finished one, is a no-op | `::test_cancelling_a_turn_waits_for_it_to_unwind`, `::test_cancelling_nothing_is_safe` | passing |
| TC-BE-232 | TR-062 | Given a tool call the deck refused, then the fallback may still route the answer — a rejected call is not the model navigating | `::test_a_rejected_tool_call_still_lets_the_fallback_route_the_answer` | passing|
| TC-BE-233 | PRD §8 / TR-062 | Given an off-topic question the agent declines, then the deck does not move; every phrasing of the redirect the prompt asks for is caught, and ordinary answers are not swallowed by the guard | `::test_an_off_topic_redirect_leaves_the_deck_where_it_is`, `::test_a_decline_is_recognised_however_it_is_phrased`, `::test_an_answer_that_engages_with_the_deck_is_not_a_redirect` | passing|
| TC-BE-234 | TR-050 | Given a model that speaks and then navigates, then the second request replays what was already spoken so the answer is not repeated | `::test_the_second_request_replays_what_was_already_spoken` | passing|
| TC-BE-235 | F5 | Given a turn that generates nothing, then something is still said — silence reads as a crash, and an empty turn poisons history | `::test_a_turn_that_generates_nothing_still_says_something` | passing|
| TC-BE-236 | TR-021/031/034 | Then barge-in releases the HTTP stream at once, an ordinary turn leaves no stream suspended, and a cancel between the tool call and the `slide.goto` still moves the deck | `::test_cancelling_mid_answer_closes_the_model_stream_at_once`, `::test_an_ordinary_turn_leaves_no_stream_suspended`, `::test_a_cancel_between_the_tool_call_and_the_goto_still_moves_the_deck` | passing|
| TC-BE-237 | TR-022 | Given a cancellation aimed at the waiter rather than the turn, then `cancel_task` lets it through instead of swallowing it | `::test_cancel_task_lets_a_cancellation_aimed_at_the_caller_through` | passing|
| TC-BE-242 | TR-050 | Given the second request of a navigating turn reopening with the sentence the first already spoke, then it is dropped rather than said to the room twice | `::test_a_sentence_already_spoken_this_turn_is_not_said_again` | passing |
| TC-BE-243 | TR-050 | Given the same question asked in two turns, then it is answered both times. The guard is scoped to one answer, not to the conversation | `::test_a_repeat_in_a_later_turn_is_still_allowed` | passing |

### Turn metrics

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-BE-180 | F12 / TR-163 | Given a fresh turn, then every duration is None; a half-finished stage reports None rather than zero; an end with no start is None too | `tests/test_metrics.py::test_a_fresh_turn_reports_no_duration_at_all`, `::test_a_duration_stays_none_until_both_of_its_ends_are_marked`, `::test_an_end_without_a_start_is_still_none` | passing|
| TC-BE-181 | TR-035 | Then `mark_first_token`, `mark_first_audio` and `mark_tts_request` are idempotent, so a long stream does not inflate the TTFT it reports | `::test_the_first_marks_ignore_every_later_call`, `::test_time_to_first_token_measures_the_first_token_not_the_last` | passing|
| TC-BE-182 | TR-163 | Then `to_message()` renders a valid `MetricsMsg`; a dropped or failed turn renders one full of Nones rather than raising | `::test_the_message_carries_every_timing_the_turn_recorded`, `::test_a_turn_that_produced_nothing_still_renders_a_message` | passing|
| TC-BE-183 | TR-035 | Then durations are whole, non-negative milliseconds (a backwards pair reads 0), and readings come from `perf_counter` | `::test_durations_are_whole_non_negative_milliseconds`, `::test_the_clock_is_monotonic` | passing|

### Providers (contract, recorded fixtures)

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-BE-070 | TR-082 | Given a recorded Groq SSE stream with a streamed tool call in argument fragments, when parsed, then one `ToolCallDelta` with parsed JSON args | `tests/test_groq_llm.py::test_fragmented_tool_call_arrives_as_one_parsed_delta` | passing |
| TC-BE-071 | TR-082 | Given a recorded SSE stream with text then `[DONE]`, then TokenDeltas followed by `LLMDone{stop}`, surviving keep-alive comments and non-`data` fields | `::test_text_stream_yields_tokens_then_one_done` | passing |
| TC-BE-072 | TR-082 / TR-085 | Given an HTTP 429 with `retry-after: 3`, then `ProviderError(retryable=True)` carrying 3; 5xx is retryable, 4xx is not, an unparsable retry-after gives None | `::test_error_status_becomes_a_provider_error` | passing |
| TC-BE-073 | TR-031 / TR-034 | Given a stream in progress, when the consuming task is cancelled, then the httpx response is closed (mock asserts `aclose`) | `::test_cancelling_the_consumer_closes_the_response` | passing |
| TC-BE-074 | TR-081 | Given a 1 s 16 kHz PCM buffer, when wrapped, then a valid WAV header with sample rate 16,000, 1 channel, 16-bit | superseded by `tests/test_groq_stt.py::test_the_wav_header_describes_the_audio_it_wraps` | retired — the provider landed with its own block and the test that carries this behaviour claims TC-BE-260. The ID is kept so it is never reused |
| TC-BE-075 | TR-083 | Given KokoroTTS with the real weights, when synthesising a five-word sentence, then the audio runs between 0.5 s and 4 s at 24 kHz and is not silence | `tests/test_kokoro_tts.py::test_real_synthesis_produces_audible_speech` | passing — `integration`, so outside the default run: it loads the real model |
| TC-BE-076 | TR-080 | Given `STT_PROVIDER=bogus`, when building providers, then startup fails listing the valid values — checked at both gates, pydantic and the registry, for all three provider slots | `tests/test_registry.py::test_unknown_provider_name_fails_listing_the_valid_values` | passing |
| TC-BE-077 | TR-176 | Given `LLM_PROVIDER=groq` and no `GROQ_API_KEY`, then startup fails naming `.env.example` and the variables that need the key | `::test_missing_groq_key_fails_naming_env_example` | passing |
| TC-BE-150 | TR-082 | Then the request body carries the configured model, temperature and max_tokens, and offers the deck's tools | `tests/test_groq_llm.py::test_request_carries_the_configured_generation_settings` | passing |
| TC-BE-151 | TR-082 | Given a tool call delivered whole in one delta, or with empty arguments, then it is still emitted exactly once | `::test_unfragmented_tool_calls_are_emitted_once` | passing |
| TC-BE-152 | TR-032 / TR-082 | Given fragments of two tool calls interleaved, then they are separated by index and delivered in the order the model made them | `::test_two_interleaved_tool_calls_keep_their_order` | passing |
| TC-BE-153 | TR-082 | Given an absent, unknown, or truncated finish reason — including a stream that ends without `[DONE]` — then exactly one `LLMDone` is produced with a mapped reason | `::test_finish_reason_is_mapped_onto_the_protocol_vocabulary` | passing |
| TC-BE-154 | TR-085 | Given a connect error, read timeout, or protocol error from httpx, then it is wrapped in `ProviderError` with the right retryable flag and never leaks | `::test_transport_failures_are_wrapped_not_leaked` | passing |
| TC-BE-155 | TR-176 | Given a blank key, construction fails with an actionable message; `aclose()` closes only a client the provider owns, never an injected one | `::test_construction_requires_a_key_and_aclose_respects_ownership` | passing |
| TC-BE-156 | TR-085 | Given an error frame sent mid-stream after a 200 OK, then the turn fails with a `ProviderError` rather than a silently truncated answer | `::test_mid_stream_error_frame_becomes_a_provider_error` | passing |
| TC-BE-157 | TR-080 | Then every selectable provider satisfies its runtime-checkable protocol, and the fakes the suite injects do too | `tests/test_registry.py::test_selectable_providers_satisfy_their_protocols_and_the_fakes_run` | passing |
| TC-BE-158 | TR-080 | Given a provider that has not shipped yet (Groq/local STT, Kokoro TTS), then the failure names the implementation and the milestone it arrives in | `::test_providers_from_later_milestones_say_when_they_arrive` | passing |
| TC-BE-159 | §3.5 | Given `fake`, then the registry never imports the Groq module; given production selections, it never imports the fakes (checked in a clean interpreter) | `::test_selecting_fake_never_imports_groq_and_production_never_imports_fakes` | passing |
| TC-BE-162 | TR-082 | Given two successive requests, then the body is finished past `[DONE]` so httpcore can pool the connection and the second reuses it | `tests/test_groq_llm.py::test_successive_requests_share_one_tcp_connection` | passing |
| TC-BE-163 | TR-082 | Given U+2028/U+2029 inside a frame, then it does not split the SSE line — only CR, LF and CRLF end one, though `str.splitlines` ends on six more | `::test_a_unicode_line_break_inside_a_frame_does_not_split_it` | passing |
| TC-BE-164 | TR-082 | Given LF, CRLF or CR as the server's terminator, then each ends a line; a CR ending a network read is held back until its LF arrives | `::test_all_three_sse_line_terminators_are_understood`, `::test_a_crlf_split_across_two_reads_is_one_terminator` | passing |
| TC-BE-165 | TR-082 | Given fragments carrying neither index nor id, then they continue the call already open — providers streaming one call at a time need not repeat its identity | `::test_a_tool_call_without_an_index_survives_being_fragmented` | passing |
| TC-BE-166 | TR-082 | Given a call already emitted, then the next unidentified fragment starts a new one instead of being appended to the last | `::test_a_completed_call_does_not_swallow_the_next_unindexed_one` | passing |
| TC-BE-167 | TR-031 | Given frames after `[DONE]`, then the drain reads and discards them; a body that never ends does not hold up the turn, because draining is best-effort | `::test_frames_arriving_after_done_are_read_and_discarded`, `::test_a_body_that_never_ends_does_not_hold_up_the_turn` | passing |
| TC-BE-168 | TR-085 | Given a hung upstream, then the owned client's connect/write/read/pool budgets fail the request on their own, not only via the 20 s turn watchdog | `::test_the_owned_client_bounds_every_phase_of_a_request` | passing |
| TC-BE-178 | TR-013 | Then `aclose` closes a provider that holds something and steps over those that do not — only the Groq provider owns an httpx pool | `tests/test_registry.py::test_closing_the_providers_releases_the_ones_that_hold_something` | passing|
| TC-BE-179 | TR-013 | Given application shutdown, then the lifespan closes the providers it built and gives back the LLM's connection pool | `::test_the_lifespan_closes_the_providers_it_built` | passing|

### A capture that could never end (added 2026-09-11)

Found by using the product: interrupting a walkthrough by speaking did nothing, however loudly.
The cause was not the interrupt path, which works; it was that no onset could be declared, because
the detector was already inside a capture that the agent's own leaked voice kept alive.

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-FE-200 | TR-116 | Given a capture opened before the agent spoke, when the agent starts speaking, then the capture is abandoned and reported as a misfire | `src/audio/microphone.test.ts::TC-FE-200` | passing |
| TC-FE-201 | TR-116 | And because it was abandoned, a later onset is declared, so the listener can interrupt | `::TC-FE-201` | passing |
| TC-FE-202 | TR-116 | Given 20 s of unbroken sound while the agent is audible, then it is discarded rather than transcribed as a question | `::TC-FE-202` | passing |
| TC-FE-203 | TR-116 | Given the same with nothing playing, then it is uploaded: a noisy room is not a stuck detector | `::TC-FE-203` | passing |
| TC-FE-204 | TR-116 | Then an ordinary barge-in is unaffected | `::TC-FE-204` | passing |
| TC-FE-205 | TR-116 | Then a question asked in the quiet after an answer is unaffected | `::TC-FE-205` | passing |
| TC-FE-206 | TR-116 / F7 | Given a false onset before the agent speaks, then the abandoned capture is followed by a real onset that does interrupt: the whole sequence, at the session seam | `src/session/bargein.test.tsx::TC-FE-206` | passing |
| TC-FE-207 | TR-116 / TR-024 | Given an abandoned capture that had already silenced the agent, then `interrupt.cancel` is sent so the server is not left waiting for a question | `::TC-FE-207` | passing |

### The eval harness (added 2026-09-11)

The suites themselves call real models and are not tests. Everything around them is: how a dataset
is read, how a style failure is recognised, how a threshold is compared, how a judge's reply is
parsed. Those are the parts that decide whether a recorded number means what it claims.

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-BE-290 | TRD §13.1 | Then each dataset meets the size the design states: routing ≥ 40, interruption ≥ 15, grounded ≥ 25 | `tests/test_evals.py::test_every_dataset_meets_the_size_the_trd_requires` | passing |
| TC-BE-291 | TR-203 | Then ids are unique within a file, so no result silently overwrites another | `::test_dataset_ids_are_unique_within_a_file` | passing |
| TC-BE-292 | TRD §13.1 | Then the routing set covers every category in the stated proportions | `::test_the_routing_set_covers_every_category_the_design_names` | passing |
| TC-BE-293 | TRD §13.1 | Then at least five grounded items are unanswerable, so the decline rate has something to measure | `::test_the_grounded_set_includes_questions_the_deck_cannot_answer` | passing |
| TC-BE-294 | TR-203 | Then every slide index a dataset names exists in the deck | `::test_every_slide_a_dataset_names_exists_in_the_deck` | passing |
| TC-BE-295 | TR-203 | Then comments and blank lines in a dataset are not read as data | `::test_comments_and_blank_lines_are_not_data` | passing |
| TC-BE-296 | TR-200 | Given `--limit`, then a dataset is capped, so a runner change can be proved without a full run | `::test_the_limit_caps_a_dataset_for_a_cheap_smoke_run` | passing |
| TC-BE-297 | E4 | Then an ordinary spoken answer passes the style check | `::test_speech_passes_the_style_check` | passing |
| TC-BE-298 | E4 | Then markdown, bullets, links, code spans and emoji all fail it | `::test_anything_that_reads_as_written_fails_the_style_check` | passing |
| TC-BE-299 | E4 | Then an answer past 90 words or 5 sentences fails on length | `::test_an_answer_that_runs_long_fails_on_length` | passing |
| TC-BE-300 | E4 | Then E4 is derived from answers the other suites produced and needs no model calls of its own | `::test_the_style_suite_reads_every_answer_the_other_suites_produced` | passing |
| TC-BE-301 | TRD §13.1 | Then a `>=` threshold and a `<=` threshold are compared in the directions they were written | `::test_a_threshold_is_compared_in_the_direction_it_was_written` | passing |
| TC-BE-302 | TRD §13.1 | Then a metric that was never measured fails its threshold rather than passing by absence | `::test_a_metric_that_was_never_measured_does_not_silently_pass` | passing |
| TC-BE-303 | TR-202 | Then a suite whose items all failed still reports rather than dividing by zero | `::test_an_empty_denominator_is_zero_rather_than_an_error` | passing |
| TC-BE-304 | TR-202 | Then the summary names each metric in its own units, with the git SHA and the model | `::test_the_summary_names_each_metric_with_its_units` | passing |
| TC-BE-305 | E6 | Then an item the provider refused is excluded from the off-topic rate rather than counted | `::test_tool_hygiene_ignores_items_the_provider_refused` | passing |
| TC-BE-306 | TR-201 | Given a judge that wraps its JSON in prose and a code fence, then the score is still read | `::test_a_judge_reply_is_read_even_when_it_is_wrapped_in_prose` | passing |
| TC-BE-307 | TR-201 | Given a reply with no JSON, then the item is ungradeable rather than scored zero | `::test_a_reply_with_no_json_is_an_error_rather_than_a_zero` | passing |
| TC-BE-308 | TR-201 | Given unparseable JSON, then the raw reply is kept so the verdict can be read back | `::test_unparseable_json_is_reported_with_what_was_said` | passing |
| TC-BE-309 | TR-201 | Given a judge whose provider fails, then a verdict is returned rather than the run ending | `::test_a_judge_whose_provider_fails_returns_a_verdict_not_an_exception` | passing |
| TC-BE-310 | TR-201 | Then the rubric is the system message and the item is the user message, not the other way round | `::test_the_rubric_is_the_system_message_and_the_case_is_the_user_message` | passing |

### Rate limiting, the single-process build, and resuming a tour (added 2026-09-11)

The free tier's ceiling is reachable in ordinary use, so what the app does about it is a feature
rather than an edge case. The other two rows here close the last of milestone M4.

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-BE-283 | TR-171 | Given a provider failure carrying a `retry-after`, then the error is `rate_limited` and `retry_after_s` carries the wait as a number | `tests/test_session.py::test_a_rate_limit_reports_how_long_to_wait` | passing |
| TC-BE-289 | TR-171 | Given a wait of 877 seconds, then the message says "about 15 min" rather than a four-figure count of seconds | `tests/test_session.py::test_a_long_wait_is_stated_in_minutes` | passing |
| TC-BE-284 | TR-171 | Given any other provider failure, then `retry_after_s` is null, so no countdown is offered for something that will not fix itself | `::test_an_ordinary_failure_carries_no_wait` | passing |
| TC-BE-285 | TR-212 | Given `frontend/dist` exists, then `/` serves the app and `/api` still answers JSON | `tests/test_startup.py::test_the_built_frontend_is_served_at_the_root_when_it_exists` | passing |
| TC-BE-286 | TR-212 | Given no build, then nothing is mounted and the API is unaffected | `::test_without_a_build_the_root_is_simply_not_served` | passing |
| TC-BE-287 | F8 | Given an interrupted walkthrough, when the user says \"carry on\", then it resumes at or after the slide the client last saw, and never back at slide one. A floor rather than an equality: the cursor advances before the slide is announced, so an interrupt landing in that window leaves the server one ahead of the client | `tests/test_session.py::test_carry_on_resumes_the_walkthrough_where_it_was_cut` | passing |
| TC-BE-288 | F8 | Given no walkthrough, then the same words are an ordinary question for the model | `::test_carry_on_outside_a_walkthrough_is_an_ordinary_question` | passing |
| TC-FE-180 | TR-171 | Given a wait of 12 s, then the chip says how long the free tier asked us to wait | `src/components/RateLimitChip.test.tsx::TC-FE-180` | passing |
| TC-FE-181 | TR-171 | Then it counts down as the wait passes | `::TC-FE-181` | passing |
| TC-FE-182 | TR-171 | Then it disappears when the wait is over, leaving no timer running | `::TC-FE-182` | passing |
| TC-FE-183 | TR-171 | Given no wait, then nothing is shown and no timer starts | `::TC-FE-183` | passing |
| TC-FE-184 | TR-171 | Given a wait that already elapsed, then nothing is shown | `::TC-FE-184` | passing |
| TC-FE-185 | TR-171 | Given a second, longer wait, then the chip shows the longer one | `::TC-FE-185` | passing |
| TC-FE-186 | TR-171 | Given `error{rate_limited, retry_after_s}`, then the store records the instant it will be ready | `src/store.test.ts::TC-FE-186` | passing |
| TC-FE-187 | TR-171 | Given any other error, then no countdown is recorded | `::TC-FE-187` | passing |
| TC-FE-188 | TR-171 | Given a shorter wait after a longer one, then the longer one stands | `::TC-FE-188` | passing |
| TC-FE-189 | TR-171 | Then the rate limit is still logged like any other error | `::TC-FE-189` | passing |
| TC-FE-190 | TR-115 | Then text fields, textareas, selects and content-editable elements are typing targets | `src/keyboard.test.ts::TC-FE-190` | passing |
| TC-FE-191 | TR-115 | Then a checkbox is **not** a typing target, so ticking "debug" does not disable the arrow keys | `::TC-FE-191` | passing |
| TC-FE-192 | TR-115 | Then ordinary elements and non-elements are not typing targets | `::TC-FE-192` | passing |
| TC-FE-193 | TR-115 | Then buttons, checkboxes, radios and `role="button"` are space-activated | `::TC-FE-193` | passing |
| TC-FE-194 | TR-115 | Then a text field is not space-activated | `::TC-FE-194` | passing |
| TC-FE-195 | TR-115 | Given a focused button, when space is held, then the button is pressed and no turn starts | `src/session/usePushToTalk.test.tsx::TC-FE-195` | passing |
| TC-FE-196 | TR-115 | Given a focused checkbox, when space is held, then it ticks and no turn starts | `::TC-FE-196` | passing |
| TC-FE-197 | TR-115 | Given the push-to-talk toggle clicked with a mouse, then it releases focus so the space bar is free | `src/components/Controls.test.tsx::TC-FE-197` | passing |
| TC-FE-199 | TR-171 | Then a wait past 90 seconds is shown in minutes, on both the helper and the chip | `src/components/RateLimitChip.test.tsx::TC-FE-199` | passing |
| TC-FE-198 | TR-115 | Given it activated from the keyboard, then focus is kept so the same key turns it off | `::TC-FE-198` | passing |

### The audio path (added 2026-09-11)

`handle_utterance` is the one part of the session a typed question never reaches: the size guard,
the transcription call, and the two ways a transcript can be worth nothing. These rows drive it the
way a browser does, with `speech.end` followed by the bytes.

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-BE-281 | F4 / TR-030, TR-140 | Given `speech.end` then an utterance, then the transcript becomes the question the model is asked, and `metrics.stt_ms` reports the transcriber's own latency | `tests/test_session.py::test_a_spoken_question_is_transcribed_and_answered` | passing |
| TC-BE-282 | TR-020 | Given an utterance being transcribed, then the session stays in HEARING and announces exactly one THINKING, carrying the id of the turn the utterance opened | `::test_the_session_stays_in_hearing_while_the_transcriber_works` | passing |

### Provider adapters (added 2026-09-11)

The Groq Whisper and Kokoro adapters arrived with M3 and M2 and claimed blocks of their own.
Neither block touches the network or the model: the transcriber is driven through an
`httpx.MockTransport` and the synthesiser through a stand-in that returns a fixed tone, so what is
exercised is everything the adapter owns around the vendor call. Two cases do need the real 340 MB
weights and skip when they are absent: TC-BE-280 here, and TC-BE-075 in the table above.

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-BE-260 | F4 / TR-030 | Given PCM16 at 16 kHz, when wrapped, then the WAV header reports one channel, 16-bit, 16,000 Hz, and the samples come back byte for byte. A wrong container mis-pitches what Whisper hears | `tests/test_groq_stt.py::test_the_wav_header_describes_the_audio_it_wraps` | passing |
| TC-BE-261 | TR-030 | Given a zero-length recording, then the container still parses rather than producing a file that fails to open | `::test_an_empty_recording_still_produces_a_valid_container` | passing |
| TC-BE-262 | F4 / TR-172 | Given audio under `MIN_UTTERANCE_BYTES` (empty, one sample, one sample short), then the transcript is empty and no request is made. Whisper answers a click with a confident hallucination, and the request would be billed for it | `::test_audio_too_short_to_be_speech_is_never_uploaded` | passing |
| TC-BE-263 | F4 / TR-081 | Given a successful transcription, then the text is trimmed and its cost reported, and the upload is a real WAV posted to `/audio/transcriptions` with the bearer key and a pinned language | `::test_a_successful_transcription_returns_the_text_and_its_cost` | passing |
| TC-BE-264 | TR-172 | Given a transcript of nothing but whitespace, then it comes back empty; silence is not an error | `::test_a_transcript_of_only_whitespace_comes_back_empty` | passing |
| TC-BE-265 | TR-085 / TR-171 | Given HTTP 429 with `retry-after: 3`, then one retry is made and no more, and the `ProviderError` is retryable and carries the 3 | `::test_a_rate_limit_is_retried_once_and_then_reported` | passing |
| TC-BE-266 | TR-085 | Given a 503 that clears on the second attempt, then the transcript is returned and nothing is reported. The retry exists to be useful, not only to delay the error | `::test_a_transient_failure_that_clears_is_not_reported` | passing |
| TC-BE-267 | TR-085 | Given HTTP 400, then it is not retried and the error is not retryable; the same request would fail the same way | `::test_a_rejected_request_is_not_retried` | passing |
| TC-BE-268 | TR-085 | Given a transport failure, then it is wrapped as a retryable `ProviderError` and no httpx exception crosses the provider boundary | `::test_a_transport_failure_becomes_a_provider_error` | passing |
| TC-BE-269 | TR-085 | Given a 200 whose body is an HTML gateway page, then the failure says the body is not JSON instead of surfacing a decoding traceback | `::test_a_response_that_is_not_json_is_reported_clearly` | passing |
| TC-BE-270 | TR-013 | Then `aclose()` releases the connection pool the provider owns | `::test_closing_releases_the_connection_pool` | passing |
| TC-BE-271 | TR-083 | Given a sample just past ±1.0, then it is clipped before scaling, so it cannot wrap into the loudest possible sample | `tests/test_kokoro_tts.py::test_samples_are_clipped_before_scaling` | passing |
| TC-BE-272 | §6.1 / TR-083 | Then conversion produces exactly two bytes per sample; a stray byte would desynchronise playback for the rest of the turn | `::test_conversion_produces_two_bytes_per_sample` | passing |
| TC-BE-273 | TR-141 | Given audio of two whole frames and a remainder, then three frames are yielded, only the last is short, and every frame holds whole samples | `::test_audio_is_yielded_in_wire_sized_frames` | passing |
| TC-BE-274 | TR-083 | Given blank text, then no frame is emitted and the model is never asked | `::test_blank_text_produces_no_audio_at_all` | passing |
| TC-BE-275 | F1 / TR-083 | Given a deck that names its own voice, then that voice and the configured speed are what reach the model | `::test_the_configured_voice_and_speed_reach_the_model` | passing |
| TC-BE-276 | TR-012 / TR-085 | Given synthesis attempted before `warm_up`, then a `ProviderError` names the missing step rather than raising an attribute error | `::test_synthesising_before_warm_up_is_refused_clearly` | passing |
| TC-BE-277 | TR-083 | Given the model returning 22,050 Hz, then it is refused naming 24000. The client's audio clock is fixed, so a mismatch plays at the wrong pitch | `::test_an_unexpected_sample_rate_is_refused` | passing |
| TC-BE-278 | TR-085 | Given the library raising, then a `ProviderError` carries the original as `__cause__` and nothing vendor-shaped reaches the pipeline | `::test_a_model_failure_does_not_escape_as_a_vendor_error` | passing |
| TC-BE-279 | TR-012 | Given the weights missing with downloads off, then the failure names both files, so an operator knows what to fetch and where | `::test_missing_weights_with_downloads_off_says_which_files` | passing |
| TC-BE-280 | TR-012 | Given the weights on disk, then their SHA-256 digests match the ones this code was written against, so a truncated download fails loudly rather than sounding wrong | `::test_the_shipped_weights_match_the_digests_this_code_was_written_against` | passing — skipped when the weights are absent |

### Protocol

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-BE-080 | TR-142 | Given `{"type": "speech.end"}` without `duration_ms`, then validation fails and `error{bad_message}` names the field; absent, mistyped and out-of-range fields are all refused, and a well-formed message still validates | `tests/test_protocol.py::test_a_speech_end_without_a_duration_is_rejected`, `::test_a_malformed_client_message_never_validates`, `::test_a_well_formed_message_still_validates` | passing |
| TC-BE-081 | TR-142 | Given `text.input` with 501 chars, then `bad_message`; 500 and 1 are accepted, and the over-long frame starts no turn | `::test_a_question_at_the_cap_is_accepted_and_one_past_it_is_not`, `::test_an_overlong_question_reaches_the_client_as_bad_message` | passing |
| TC-BE-082 | §6.1 | Given sentence_id 7, seq 3, payload b"..", when framed, then bytes start with `07 00 00 00 03 00 00 00` | `::test_the_audio_header_is_two_little_endian_uint32s` | passing |
| TC-BE-185 | §6.1 | Property (hypothesis): `decode(encode(x))` is `x` for every id, sequence and payload | `::test_any_frame_survives_the_round_trip` | passing |
| TC-BE-186 | §6.1 | Given a frame of 0–7 bytes, then decoding raises rather than returning garbage; exactly eight bytes is a valid, empty frame | `::test_a_frame_too_short_to_hold_a_header_is_refused`, `::test_a_header_with_no_payload_decodes_to_empty_audio` | passing |
| TC-BE-187 | TR-143 | Then `CLIENT_MESSAGE_TYPES` and `SERVER_MESSAGE_TYPES` are derived from the model unions, so a new message cannot be added without appearing in them | `::test_the_exported_type_sets_match_the_message_unions` | passing |
| TC-PAR-001 | TR-143 | Then the set of `type` literals in `protocol.py` equals the set in `protocol.ts`, and the TypeScript arrays are checked against the TypeScript unions by `satisfies` | `tests/test_protocol_parity.py::test_the_two_languages_declare_the_same_message_types`, `::test_typescript_checks_its_own_arrays_against_its_own_unions` | passing |
| TC-PAR-002 | TR-143 | Then the shared constants agree across the two files: protocol version, audio header size, and the text-input cap | `::test_the_shared_constants_agree` | passing |
| TC-PAR-003 | TR-143 | Then every closed value set agrees: session states, error codes, session modes, tool sources and control actions | `::test_every_closed_value_set_agrees` | passing |

### HTTP routes

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-BE-190 | F1 / TR-151 | Given the app is started, when `GET /api/decks`, then one summary per deck, ordered by id | `tests/test_routes.py::test_the_deck_listing_names_the_shipped_deck` | passing |
| TC-BE-191 | F1 / TR-151 | When `GET /api/decks/{id}`, then the whole deck the agent presents is returned | `::test_a_deck_is_served_whole` | passing |
| TC-BE-192 | TR-151 | Given an id no deck carries, then 404 naming the ids that do exist | `::test_an_unknown_deck_is_a_404_that_names_what_does_exist` | passing |
| TC-BE-193 | TR-151 | Given the lifespan has not run, then both deck routes answer 503 — not `200 []` or 404 — while `/api/health` still answers | `::test_the_deck_routes_answer_503_before_startup_completes`, `::test_health_still_answers_before_startup` | passing |
| TC-BE-194 | TR-026 | Given the lifespan has not run, when a client connects to `/ws/session`, then the socket closes with 1011 rather than accepting and hanging | `::test_the_session_socket_refuses_a_connection_before_startup_completes` | passing |

---

## Backend integration (require `GROQ_API_KEY`, `-m integration`)

The package landed on 2026-09-11 with one test for each row. They are marked `integration` and
deselected by default (`addopts = ["-m", "not integration"]`), so a plain `make test` never spends
quota; `make test-integration` runs them against a real key read from `.env`. This catalogue has
not run them, so they are `implemented` rather than `passing`: the row says a test exists and
asserts the behaviour, not that it was green on a given day.

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-INT-001 | F4 / TR-030 | Given `tests/fixtures/audio/how_do_you_handle_interruptions.wav`, when transcribed by the real endpoint, then the text contains "interrupt" and the latency is reported | `tests/integration/test_groq_stt.py::test_a_spoken_question_comes_back_as_words` | passing — run live 2026-09-11 |
| TC-INT-002 | F4 / TR-172 | Given `tests/fixtures/audio/silence_2s.wav`, then the transcript is empty or something `is_filler` already drops; Whisper hallucinates on silence more often than it returns nothing | `::test_two_seconds_of_silence_produce_nothing_to_answer` | passing — run live 2026-09-11 |
| TC-INT-003 | F5 | Given the real model and "how do you handle interruptions?", then the deck ends on slide 4, routed by the model rather than by a script | `tests/integration/test_pipeline.py::test_a_question_about_interruptions_moves_the_deck_to_the_slide_about_them` | passing — run live 2026-09-11 |
| TC-INT-004 | F5 / PRD §8 | Given "what is the weather in London today?", then the deck does not move and the answer is at most three sentences; brevity matters as much as the refusal | `::test_a_question_the_deck_does_not_answer_is_declined_without_moving_it` | implemented — not yet run: the free tier's daily token budget for the configured model was spent |
| TC-INT-005 | F6 / TR-125 | Given the real pipeline driven over a WebSocket, then the first audio frame leaves within 1,500 ms of the question | `::test_a_question_is_answered_out_loud_within_the_latency_budget` | passing — run live 2026-09-11, first audio 777 ms |

---

## Frontend unit (vitest)

The audio rows (TC-FE-010–014, TC-FE-020–023, TC-FE-035) are all implemented. Playback is in
`src/audio/playback.test.ts`; capture and detection are in `src/audio/microphone.test.ts`, because
the module that shipped is `microphone.ts` rather than the `vad.ts` and `capture.ts` these rows
once named. The two rows that span the microphone and the socket, TC-FE-021 and half of
TC-FE-022, are driven by `src/session/bargein.test.tsx`, which wires the real hook to the real
playback queue and replaces only the microphone and the audio device.

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-FE-001 | F1 | Given the deck, when rendering `SlideDeck` at index 3, then slide 3 title is visible and dot 3 is active | `src/components/SlideDeck.test.tsx::TC-FE-001` | passing |
| TC-FE-002 | F1 / TR-133 | Given `slide.goto{index: 42}`, then the store clamps to 6 | `src/store.test.ts::TC-FE-002` | passing |
| TC-FE-003 | F1 | When ArrowRight is pressed with a live session, then `slide.changed{source: user}` is sent | `src/session/useSession.test.tsx::TC-FE-003` | passing |
| TC-FE-010 | F6 / TR-121 | Given consecutive frames, when scheduled on a fake `AudioContext`, then each `start(when)` equals the previous end time (no gaps, no overlap) | `src/audio/playback.test.ts::TC-FE-010` | passing |
| TC-FE-011 | TR-121 | Given a frame arriving after the clock has run past the end of the last one, then it is scheduled from the current time rather than overlapping what is already playing | `::TC-FE-011` | passing |
| TC-FE-012 | F7 / TR-122 | Given 5 scheduled sources, when `flush()`, then every source's `stop` is called and the queue is empty | `::TC-FE-012` | passing |
| TC-FE-013 | TR-123 | Given sentence 0 frames then the first frame of sentence 1, then `onSentenceComplete(0)` fires exactly once, and not before its own frames have played | `::TC-FE-013` | passing |
| TC-FE-014 | TR-123 | Given the final sentence and then the turn sealing the queue, then `onSentenceComplete(last)` fires. Nothing later will arrive to prove it finished | `::TC-FE-014` | passing |
| TC-FE-020 | F3 / TR-112 | Given `isPlaying=true` and 2 consecutive positive frames, then no onset; on the 3rd, onset. A lone loud frame among quiet ones never reaches onset | `src/audio/microphone.test.ts::TC-FE-020` | passing |
| TC-FE-021 | TR-113 | Given onset while playing, then `flush()` is called before `interrupt` is sent, and `interrupt.last_completed_sentence_id` equals the queue's last completed id; the time the audio took to stop is recorded for the latency panel | `src/session/bargein.test.tsx::TC-FE-021` | passing |
| TC-FE-022 | TR-114 | Given an utterance under the 250 ms minimum, then nothing is uploaded; if an interrupt was sent, `interrupt.cancel` follows, and if the agent was already silent, nothing is sent at all | `src/audio/microphone.test.ts::TC-FE-022`, `src/session/bargein.test.tsx::TC-FE-022` | passing |
| TC-FE-023 | TR-114 | Given onset then end, then the emitted utterance length equals pre-pad + speech within one frame, and the closing silence is not uploaded | `src/audio/microphone.test.ts::TC-FE-023` | passing |
| TC-FE-030 | §6.1 | Given a binary frame with header (7, 3), when decoded, then `{sentenceId: 7, seq: 3, pcm}` | `src/protocol.test.ts::TC-FE-030` | passing |
| TC-FE-031 | TR-131 | Given store turnId 5 and an incoming `transcript.agent{turn_id: 4}`, then it is ignored — while `state` still passes, being the message that reports the turn being left | `src/store.test.ts::TC-FE-031` | passing |
| TC-FE-032 | F10 / TR-132 | Given `agent.cancelled{truncated_at: 1}` after sentences 0–3 were logged, then entries 2 and 3 are marked unheard | `::TC-FE-032` | passing |
| TC-FE-033 | F12 | Given three metrics messages, then the HUD shows the last turn's value beside the session median | `src/components/LatencyHUD.test.tsx::TC-FE-033` | passing |
| TC-FE-034 | TR-175 | Given an abnormal close (1006), then exactly one reconnect attempt with a new `session.start` | `src/session/client.test.ts::TC-FE-034` | passing |
| TC-FE-035 | TR-103 | Given five start/stop cycles with a fake `MediaStream`, then every track's `stop()` was called and all contexts closed | `src/audio/microphone.test.ts::TC-FE-035` | passing |
| TC-FE-100 | §6.1 | Given multi-byte header fields and full-range samples, then encode/decode round-trips them | `src/protocol.test.ts::TC-FE-100` | passing |
| TC-FE-101 | §6.1 | Given a frame shorter than the header, or one whose last sample is truncated, then decoding rejects it | `::TC-FE-101` | passing |
| TC-FE-102 | TR-143 | Given a well-formed server message, then `parseServerMessage` narrows it to its discriminated type | `::TC-FE-102` | passing |
| TC-FE-103 | TR-143 | Given an unknown `type` or malformed JSON, then it returns null rather than throwing | `::TC-FE-103` | passing |
| TC-FE-104 | TR-143 | Given JSON that is not an object with a string `type`, then it returns null | `::TC-FE-104` | passing |
| TC-FE-105 | TR-143 | Then the exported type arrays list every message type exactly once — the half of the parity test that lives in TypeScript | `::TC-FE-105` | passing |
| TC-FE-106 | F2 / TR-130 | Given `session.ready`, then the store holds the TR-130 shape (session id, deck, providers) and logs an entry | `src/store.test.ts::TC-FE-106` | passing |
| TC-FE-107 | TR-130 | Given a `state` message, then the turn advances and the state it left is timed | `::TC-FE-107` | passing |
| TC-FE-108 | F10 | Given user, agent, tool and error messages, then the log holds one entry each, in arrival order | `::TC-FE-108` | passing |
| TC-FE-109 | TR-133 | Given an out-of-range `slide.goto`, then the clamp is recorded as a client event instead of failing | `::TC-FE-109` | passing |
| TC-FE-110 | F12 / TR-162 | Given several `metrics` messages, then the store keeps the last sample, the rolling medians, and the client-measured timings | `::TC-FE-110` | passing |
| TC-FE-111 | §7.2 | Then the debug export is the TRD §7.2 envelope: the raw messages plus `client_ts` | `::TC-FE-111` | passing |
| TC-FE-140 | TR-131 | Given a `state` message from a superseded turn, then the store's turn counter never rewinds | `::TC-FE-140` | passing |
| TC-FE-141 | F2 / TR-131 | Given `session.ready` for a reconnected session, then the turn counter restarts | `::TC-FE-141` | passing |
| TC-FE-142 | F1 / TR-131 | Given `slide.goto` carrying a superseded `turn_id`, then the deck does not move | `::TC-FE-142` | passing |
| TC-FE-143 | F2 | Given `clearSession`, then the session is dropped but the user's toggles survive | `::TC-FE-143` | passing |
| TC-FE-144 | F10 | Given a client notice, then it is quiet by default and can be marked as an alert | `::TC-FE-144` | passing |
| TC-FE-112 | TR-175 | Given the one automatic reconnect also fails, then the client gives up and reports it | `src/session/client.test.ts::TC-FE-112` | passing |
| TC-FE-113 | TR-103 | Given `close()`, then every listener is removed, a pending retry is cancelled, and it never reconnects | `::TC-FE-113` | passing |
| TC-FE-114 | TR-175 | Given an explicit close of an open socket, then that is normal and no retry is due | `::TC-FE-114` | passing |
| TC-FE-115 | TR-143 | Then text frames are parsed, unknown ones dropped, and binary frames passed through untouched | `::TC-FE-115` | passing |
| TC-FE-120 | F11 | Given each session state, then the orb has its own visual state and label | `src/components/Orb.test.tsx::TC-FE-120` | passing |
| TC-FE-121 | F11 / TR-134 | Then the state is announced through a live region, not only through colour | `::TC-FE-121` | passing |
| TC-FE-122 | F11 | Given the compact orb, then the hint line is dropped | `::TC-FE-122` | passing |
| TC-FE-147 | F7 / F11 | Given an interrupted turn, then the orb portrays it; given a state it does not recognise, then it names it rather than rendering blank | `::TC-FE-147` | passing |
| TC-FE-123 | F5 | Given a highlighted bullet, then it is emphasised and released again after four seconds | `src/components/Slide.test.tsx::TC-FE-123` | passing |
| TC-FE-124 | F5 | Given a second highlight, then the emphasis moves and its clock restarts | `::TC-FE-124` | passing |
| TC-FE-146 | F5 | Given the same bullet highlighted a second time, then it is emphasised again rather than staying dormant | `::TC-FE-146` | passing |
| TC-FE-125 | F1 / F9 | Given arrow keys, then the deck moves, and stays silent at either end | `src/components/SlideDeck.test.tsx::TC-FE-125` | passing |
| TC-FE-126 | F13 | Given focus in the question field, then arrow keys are left to the text input | `::TC-FE-126` | passing |
| TC-FE-127 | F1 / F9 | Given a click on a dot or an arrow button, then the deck navigates | `::TC-FE-127` | passing |
| TC-FE-128 | F10 | Given one event of each kind, then the log renders one entry per kind in arrival order | `src/components/EventLog.test.tsx::TC-FE-128` | passing |
| TC-FE-129 | F5 / F10 | Given `tool.call{source: llm}` and `{source: fallback}`, then the log tells a model tool call apart from a keyword fallback | `::TC-FE-129` | passing |
| TC-FE-130 | F10 / TR-132 | Given `agent.cancelled`, then the sentences the user never heard render struck through | `::TC-FE-130` | passing |
| TC-FE-131 | F12 | Given metrics and client notices, then they stay behind the debug toggle | `::TC-FE-131` | passing |
| TC-FE-132 | §7.2 | Given the copy button, then the replayable envelope reaches the clipboard and the UI says so | `::TC-FE-132` | passing |
| TC-FE-145 | F10 | Given a notice marked as an alert, then it shows even with the debug toggle off | `::TC-FE-145` | passing |
| TC-FE-133 | F13 | Given a typed question with surrounding whitespace, then the trimmed text is sent and the field emptied | `src/components/Controls.test.tsx::TC-FE-133` | passing |
| TC-FE-134 | F13 | Given an empty question, or any question with no session, then nothing is sent | `::TC-FE-134` | passing |
| TC-FE-135 | F2 | Given the controls, then Start and End drive the session, and a deck picker appears only when there is more than one deck | `::TC-FE-135` | passing |
| TC-FE-151 | F13 | Given the agent is still answering, then the composer waits rather than cancelling the answer with a second question | `::TC-FE-151` | passing |
| TC-FE-136 | F13 | Given a typed question, then it is sent as `text.input`, trimmed and capped at the protocol limit | `src/session/useSession.test.tsx::TC-FE-136` | passing |
| TC-FE-137 | TR-103 | Given the hook unmounts, then its socket is closed and anything arriving after is ignored | `::TC-FE-137` | passing |
| TC-FE-138 | F11 / TR-130 | Given server messages, then they are pushed into the store and the orb follows them | `::TC-FE-138` | passing |
| TC-FE-148 | F2 | Given a session is started, then the user's toggles are kept | `::TC-FE-148` | passing |
| TC-FE-149 | F2 / TR-175 | Given a connection the client has given up on, then it is explained where the user can see it | `::TC-FE-149` | passing |
| TC-FE-150 | F13 | Given the agent is still answering, then a second question is refused | `::TC-FE-150` | passing |

### Push-to-talk and the microphone (added 2026-09-11)

Push-to-talk (TR-115) and most of `src/audio/microphone.ts` shipped with tests that carry no ID of
their own. The IDs below are assigned here, starting at TC-FE-160 so that nothing already claimed
is disturbed; the test files should take them up the next time they are touched. Until then the
locations name each test by its sentence, because that is what the file holds.

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-FE-160 | F3 / TR-115 | Given the mode is on, when Space goes down a turn opens and the hook reports the key as held; on the way up the turn closes | `src/session/usePushToTalk.test.tsx::"TR-115: opens a turn on the way down and closes it on the way up"` | passing |
| TC-FE-161 | TR-115 | Then the keypress is swallowed, so holding the key does not scroll the page out from under the speaker | `::"swallows the keypress so the page does not scroll"` | passing |
| TC-FE-162 | TR-115 | Given auto-repeat while the key is held, whether or not the browser sets the repeat flag, then only one turn is opened | `::"auto-repeat while the key is held does not open a second turn"`, `::"a repeat flag the browser forgot to set is still only one turn"` | passing |
| TC-FE-163 | F13 / TR-115 | Given focus in the composer, then Space is left to the text field; a keyup that lands there still ends a turn the key opened | `::"stands aside while the user is typing a question"`, `::"a released key still ends the turn when focus moved to the composer meanwhile"` | passing |
| TC-FE-164 | TR-115 | Given a modified Space, or any other key, then nothing opens. Those chords belong to the browser | `::"ignores a modified space, which belongs to the browser"`, `::"ignores every other key"` | passing |
| TC-FE-165 | TR-115 | Given the window loses focus mid-hold, then the turn is ended once rather than stranded, and the later keyup does not end it again | `::"losing the window while the key is down ends the turn rather than stranding it"` | passing |
| TC-FE-166 | TR-103 / TR-115 | Given the mode switched off mid-hold, or the component unmounted mid-hold, then the turn ends and the key is unbound | `::"turning the mode off mid-hold ends the turn"`, `::"unmounting mid-hold ends the turn and unbinds the key"` | passing |
| TC-FE-167 | TR-115 | Given the mode is off, then nothing is bound and the keypress is not even prevented | `::"does nothing at all while disabled"` | passing |
| TC-FE-168 | F13 / TR-115 | Given a live session, then the mode is offered and switching it on is reported to the session | `src/components/Controls.test.tsx::"offers the mode while a session is open and reports the switch"` | passing |
| TC-FE-169 | F11 / TR-115 | Given the mode is on, then the key to hold is named, and while it is held the control says the microphone is listening | `::"says which key to hold, and says when it is being held"` | passing |
| TC-FE-170 | TR-115 | Given the mode is off, or no session is open, then no key is advertised | `::"says nothing about a key when the mode is off, or when no session is open"` | passing |
| TC-FE-171 | F3 / TR-112 | Given nothing is playing, then a single positive frame is onset. The three-frame rule exists only to survive the agent's own voice | `src/audio/microphone.test.ts::"TR-112: with nothing playing, the first loud frame is onset"` | passing |
| TC-FE-172 | F3 / TR-111 | Given a quiet dip shorter than the redemption window, then the utterance continues; given a full window it ends, and the turn after it is detected too | `::"a dip in the middle of a word does not end the utterance"`, `::"a second turn is detected after the first has been uploaded"` | passing |
| TC-FE-173 | F13 / TR-103 | Given the microphone is muted, then frames are dropped and an utterance in progress is abandoned rather than uploaded half-finished; unmuting detects again | `::"drops frames while muted and detects again after unmute"`, `::"muting mid-utterance abandons it rather than uploading half a sentence"` | passing |
| TC-FE-174 | TR-103 | Given `start()` called twice at once and again after that, then one device is opened, because React double-invokes effects in development | `::"TR-103: starting twice opens one device, as React's double-invoked effects require"` | passing |
| TC-FE-175 | TR-110 | Then the capture worklet is loaded by URL from `/worklets/capture.js` rather than imported, which is what keeps detection off the main thread | `::"loads the capture worklet by URL rather than importing it"` | passing |
| TC-FE-176 | F2 / TR-103 | Given `getUserMedia` refuses, then it is reported once, no context is opened, and the microphone does not claim to be listening | `::"a refused microphone is reported once and leaves nothing open"` | passing |
| TC-FE-177 | §6.1 / TR-114 | Given float samples inside and outside ±1.0, then they scale to full range, clamp beyond it, and produce two bytes each | `::"scales to full range and clamps beyond it"`, `::"produces two bytes per sample"` | passing |
| TC-FE-178 | F2 | Given each `DOMException` a microphone can raise, then the message names the cause and the fix; anything else still quotes what went wrong | `::"%s names the cause and the fix"`, `::"an unrecognised failure still quotes what went wrong"`, `::"something that is not an error at all is still described"` | passing |

---

## End-to-end (Playwright, fake-provider backend)

No `frontend/e2e/` directory and no Playwright harness exist yet. TRD §15 puts the first
walkthrough steps in M3 and the rest in M4.

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-E2E-001 | PRD §13 | The full walkthrough scenario, steps 1–8, using text input and synthetic VAD events; asserts slide indices, log chips, and that audio is flushed on interrupt | `e2e/walkthrough.spec.ts` | passing — two tests: the scenario end to end, and that \"carry on\" resumes the tour rather than restarting it |
| TC-E2E-002 | F2 | Given the backend is down, when Start is clicked, then the error toast with a retry button is shown; when the backend comes up and retry is clicked, the session connects | `e2e/connection.spec.ts` | passing |
| TC-E2E-003 | F13 | Given mic permission denied (Playwright permission), then the text input still produces a voice answer and a slide change | `e2e/fallback.spec.ts` | passing |
| TC-E2E-004 | F9 | Given manual navigation to slide 6 then text "explain this", then the agent answers without a `slide.goto` chip | `e2e/sync.spec.ts` | passing |

---

## Phase 0 — scaffolding (implemented 2026-09-10)

Settings, health probe, and harness cases added when the scaffold landed. Several of these are
parametrised, so 18 test functions expand to 53 collected backend cases.

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

### Hand navigation outranks the fallback (added 2026-09-11)

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-BE-240 | F9 / TR-062, TR-063 | Given the user moved the deck by hand this turn, when the answer's wording scores strongly for another slide, then the keyword fallback stays silent and the deck does not move | `tests/test_slides.py::test_the_fallback_is_suppressed_after_the_user_navigates_by_hand` | passing |
| TC-BE-241 | TR-062 | Given that suppression, when the next turn begins, then the fallback works again — it lasts one turn, not for ever | `tests/test_slides.py::test_the_fallback_returns_once_a_new_turn_begins` | passing |

### Walkthrough and mute (added 2026-09-11)

The walkthrough itself is catalogued in the session table as TC-BE-059, which is where that ID
already lived; it was written out a second time here, and the two rows have been merged.

| ID | Feature / TR | Given / When / Then | Location | Status |
|---|---|---|---|---|
| TC-BE-250 | F8 | Given "walk me through it" typed or spoken, then a walkthrough starts instead of a model turn | `tests/test_session.py::test_a_spoken_request_to_walk_through_starts_the_presentation` | passing |
| TC-FE-130 | F8 | Given an open session, when the walkthrough button is pressed, then the control message is sent | `src/components/Controls.test.tsx::TC-FE-130` | passing |
| TC-FE-131 | F8 | Given no session, then neither the walkthrough nor mute is offered | `::TC-FE-131` | passing |
| TC-FE-132 | F13 | Then mute reports its state through `aria-pressed` and toggles on click | `::TC-FE-132` | passing |
| TC-FE-133 | F13 | Given a muted session, then the button offers to unmute and reads as pressed | `::TC-FE-133` | passing |

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

---

## Open reconciliation items

One thing in the tree stops this catalogue from being a clean index, and it is now confined to the
frontend. The fix is a one-line edit in each test file and belongs to whoever owns those files: a
catalogue must not renumber somebody else's tests.

### 1. Duplicate IDs — nine frontend IDs, each claimed by two unrelated tests

The eighteen backend collisions listed here before have been resolved in the tree.
`tests/test_history.py` now claims 220–231 and `tests/test_turn.py` 232–237 and 242–243, and a scan
of every backend docstring finds no ID claimed twice. Nine frontend IDs still are, all of them
created when the audio and control work landed. The convention the suite follows is one block of
numbers per module, so the second column is the claimant that should move.

| ID | First claimant (block owner) | Second claimant (should be renumbered) |
|---|---|---|
| `TC-FE-120` – `TC-FE-122` | `src/components/Orb.test.tsx` — the orb's state, its live region, and its compact form | `src/audio/playback.test.ts` — an empty frame, a flushed sentence, and audio enqueued after a flush |
| `TC-FE-123` – `TC-FE-124` | `src/components/Slide.test.tsx` — the bullet highlight and its clock | `src/audio/playback.test.ts` — close silences playback, and closing twice is safe |
| `TC-FE-130` – `TC-FE-132` | `src/components/EventLog.test.tsx` — struck-through sentences, the debug toggle, and the copy button | `src/components/Controls.test.tsx` — the walkthrough button, hiding it before a session, and mute |
| `TC-FE-133` | `src/components/Controls.test.tsx` — the trimmed question reaches `onSend` | `src/components/Controls.test.tsx` — offering to unmute once muted (the same file claims it twice) |

Free backend numbers, if a renumbering wants a block: 067–069, 078–079, 083–089, 108–109, 127–129,
138–139, 169, 184, 188–189, 195–199, 218–219, 238–239, 244–249, 251–259, and everything from 281
up. Free frontend numbers: 004–009, 015–019, 024–029, 036–089, 096–099, 116–119, 139, 152–159, and
everything from 179 up.

### 2. Fifteen frontend tests carry no ID

They exist and pass, so they are named here rather than left invisible, but they are given no ID:
the author assigns it in the same commit as the test (CLAUDE.md §3.1a). The microphone and
push-to-talk tests that used to sit in this list were given IDs TC-FE-160–178 in this
reconciliation, because a whole feature with no catalogue entry is worse than an ID assigned late;
those IDs are not yet written into the test files.

| Location | What it asserts |
|---|---|
| `src/components/Controls.test.tsx::"shows the orb's state in words next to the composer"` | the composer states the orb's state in words |
| `::"counts down the remaining characters as the limit approaches"` | the 500-character cap is counted down |
| `src/components/EventLog.test.tsx::"reports a clipboard the browser refused rather than pretending it worked"` | a refused clipboard write is reported, not swallowed |
| `::"invites the first question when nothing has happened yet"` | the empty log invites the first question |
| `src/components/LatencyHUD.test.tsx::"grades each measurement against its budget and says when a stage never reported"` | each measurement is graded against its budget |
| `::"says there is nothing to report before the first turn"` | the HUD is honest before the first turn |
| `src/components/Slide.test.tsx::"renders the slide number and title as the slide's heading"` | the slide heading carries number and title |
| `src/components/SlideDeck.test.tsx::"passes the highlight through to the slide it belongs to"` | the highlight reaches the right slide |
| `::"says so rather than crashing when the index has no slide behind it"` | an index with no slide behind it degrades gracefully |
| `src/session/useSession.test.tsx::"shows the deck and navigates it before any session is open (PRD F1)"` | the deck works before a session exists |
| `::"refuses to send a question with no session, and says so in the log"` | a question with no session is refused and logged |
| `::"survives a backend that is not running"` | the hook survives an absent backend |
| `src/session/bargein.test.tsx::"speech while the agent is silent is a plain turn, not an interruption"` | onset outside an answer sends `speech.start`, never `interrupt` |
| `::"a real utterance after an interruption is uploaded, and the cancel is not sent"` | the flush, the interrupt, the `speech.end` and the upload happen in that order |
| `::"mute and unmute reach the device, and ending the session releases it"` | the hook's mute, unmute and stop each reach the microphone, and push-to-talk starts off |

### 3. One row retired

TC-BE-074 was retired in this reconciliation. The Groq Whisper adapter shipped with a block of its
own and its WAV-header test claims TC-BE-260, so the older row describes a test that no longer
exists under that number. The ID is kept, as retired IDs always are, so that it is never reused.
Nothing else has been withdrawn. The only rows left without an implementation are the four
end-to-end ones, which wait on a Playwright harness that does not exist yet.

### 4. Backend coverage the catalogue cannot yet claim

No backend test uploads a valid utterance over the socket. `speech.end` followed by a binary frame
is exercised only in its refusal paths (TC-BE-055, TC-BE-056), so `Session.handle_utterance`'s
success path, its empty-transcript branch, and a `ProviderError` from the transcriber are reached
by no test. That is what keeps TC-BE-052, TC-BE-053 and TC-BE-054 at `partial`.
