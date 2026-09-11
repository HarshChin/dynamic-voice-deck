# Agent Evaluations

Evals measure the **behaviour of the agent with real models**, which unit tests cannot cover because model output is non-deterministic. Suite definitions live in `docs/TRD.md` §13; datasets and the runner live in `backend/evals/`. This file records the design decisions behind each suite and the results of every recorded run.

## How to run

```bash
make evals                                   # every suite, the model in .env
make evals SUITE=E1,E4,E6 MODEL=openai/gpt-oss-120b
cd backend && uv run python -m evals.run_evals --suite E1 --limit 5   # cheap smoke run
```

Each run writes `backend/evals/results/<timestamp>.json` (every item, every answer, every judge
rationale) and `<timestamp>.md` (the summary table below), and prints the table. The runner exits
non-zero when a threshold is missed, so it can gate a release.

Evals consume Groq free-tier quota, and more of it than the item counts suggest. One model call is
about 3,000 tokens (read off the 429 bodies), a turn that navigates makes two, and E2 and E3 add a
judge call each. That puts **E1 alone at roughly 200,000 tokens -- a whole day's free-tier budget
for one model** -- and the six suites together at about two and a half days'. An earlier version of
this paragraph said a third of a day; that was an underestimate by a factor of seven. The daily
ceiling is a rolling 24-hour window, so a run started against a spent budget crawls at the refill
rate rather than failing outright. Run them at milestone boundaries, not on every commit.

**Two measurement decisions worth stating.** An item the provider refused with a 429 is excluded
from the denominator rather than counted as a wrong answer: otherwise a run during a rate limit
measures the free tier instead of the agent, and the number moves for reasons that have nothing to
do with the code. And the runner waits out a per-minute limit (up to 70 seconds) but not a daily
one, because no amount of patience recovers a spent daily budget; those items are recorded as
failures and named in the run's notes.

## Suites

| Suite | Question it answers | Dataset | Threshold |
|---|---|---|---|
| E1 Slide routing | Does the agent land on the right slide, and stay put when it should? | `datasets/routing.jsonl` | accuracy ≥ 90 %, false navigation ≤ 5 % |
| E2 Interruption memory | After being cut off, does it avoid repeating what was heard and avoid referencing what was never said? | `datasets/interruption.jsonl` | repetition ≤ 10 %, phantom reference 0 % |
| E3 Groundedness | Are answers faithful to the speaker notes, and does it decline what the deck cannot answer? | `datasets/grounded.jsonl` | mean ≥ 1.7 / 2, decline ≥ 80 % |
| E4 Spoken style | Is the output speakable: short, no markup, no URLs? | derived from E1–E3 outputs | pass ≥ 95 % |
| E5 Latency | Are stage latencies within the budget in TRD §8.1? | recorded utterances, walkthrough scenario | p95 within budget |
| E6 Tool-call hygiene | Are tool calls always valid and never triggered by off-topic input? | E1 traces | 0 invalid, ≤ 5 % on off-topic |

## Dataset design notes

### E1 routing (`routing.jsonl`)

Each line: `{"id": "r001", "utterance": "...", "current_slide": 1, "expected_slide": 4, "expected_source": "llm", "category": "paraphrase"}`.

Categories and minimum counts:

| Category | Count | Example |
|---|---|---|
| direct | 8 | "Tell me about the latency budget." → 2 |
| paraphrase | 10 | "Why does it take a moment before you answer?" → 2 |
| relative | 6 | "Next.", "Go back one.", "Start over." |
| cross-reference | 4 | "Which slide talks about cost?" → 6 |
| stay | 6 | "Can you expand on that second point?" → `null` (no navigation) |
| off-topic | 6 | "What's the weather like?" → `null` |

Items are added whenever a real session mis-routes; the utterance is copied verbatim from the event log.

### E2 interruption memory (`interruption.jsonl`)

Each line provides a history fixture ending in an assistant message truncated at sentence *k* with the `[interrupted by user]` marker, the sentences that were **not** heard, and the next user utterance. The judge receives the reply and the two sentence lists and answers two yes/no questions with rationale:

1. Does the reply repeat (semantically) any heard sentence without being asked to?
2. Does the reply refer to any unheard sentence as though it had been said ("as I mentioned…")?

### E3 groundedness (`grounded.jsonl`)

Each line: question, the slide notes containing the answer, and an `answerable` flag. The judge scores 0 (contradicts or invents), 1 (partially supported), 2 (fully supported by notes). Unanswerable items pass when the reply says the deck does not cover it.

### Judge

The judge is the same LLM provider at `temperature=0` with rubric prompts in
`backend/evals/judges/`. Judge outputs are JSON, extracted from the reply rather than assumed to be
the whole of it, because models add preambles and code fences. A 10-item hand-labelled calibration
set (`datasets/judge_calibration.jsonl`) is scored on every run; judge agreement with the hand
labels must be at least 90 % or the run reports that its judged suites should not be believed.

The calibration set is deliberately adversarial in one direction: half its items are answers that
are *nearly* right, because a judge that only separates correct from absurd will score a vague
answer as fully grounded, and vagueness is the failure mode a presenter actually has.

## Model comparison

| Model | E1 accuracy | E1 false nav | E4 style | E6 invalid | Notes |
|---|---|---|---|---|---|
| **qwen/qwen3.8-27b (Groq)** | pending | pending | pending | pending | **current default.** Chosen on the smoke comparison below and on latency; its own suite run is blocked on the daily free-tier budget, see 2026-09-11 below. |
| openai/gpt-oss-120b (Groq) | **58.3 %** (24 of 40 answered) | 0.0 % | 87.5 % | 2 | rejected. Seven of eight paraphrased questions produced no visible answer at all. |
| openai/gpt-oss-20b (Groq) | — | — | — | — | untested; same reasoning-model family as the 120b. |
| **qwen2.5:7b (Ollama, local)** | **57.5 %** (40 of 40 answered) | 8.3 % | 77.5 % | 0 | **the fallback** (TR-085). Not a candidate for primary; see the run below for why it is a good fallback anyway. |

Note: `llama-3.3-70b-versatile` is no longer offered on this account; the models actually available
are `openai/gpt-oss-120b`, `openai/gpt-oss-20b`, `openai/gpt-oss-safeguard-20b`, `qwen/qwen3.8-27b`,
`qwen/qwen3.6-27b`, and the `groq/compound` pair. Every one carries the same free-tier limit of
8,000 tokens per minute, so model choice cannot buy more throughput.

### Preliminary model comparison — 2026-09-10 (smoke, not a full eval run)

Three questions through the real pipeline, same prompt, same deck, same code. This is far too small
to be an eval result; it is recorded because it changed the default model.

| Question | `gpt-oss-120b` | `qwen3.8-27b` |
|---|---|---|
| "Which slide covers tool calling?" | Navigated to 5, then called `highlight_bullet` on the second step and never spoke. Fell back to "Here's slide 5." | Navigated to 5, answered: "Slide five, right here. It's titled Thinking: Tool Calling and Intent Routing." |
| "What's the capital of France?" | Declined, but answered anyway: "the capital of France is Paris." Fixed by sharpening the prompt. | Declined cleanly, no navigation. |
| "Go back to the interruption one and tell me how it works." | Navigated to 4, then emitted 221 characters of zero-width spaces. The deck is pure ASCII, so the corruption came from the model. | Navigated to 4, answered correctly with the short opener the prompt asks for. |
| Time to first token | 1.6-2.7 s | 0.7-2.3 s |

`gpt-oss-120b` also produced the literal text `highlightbullet(1)` inside a spoken answer during an
earlier variant of the tool loop. Taken together, the default moved to `qwen/qwen3.8-27b`. This
should be re-tested properly by eval suites E1, E4 and E6 before release.

## Results log

Append one section per recorded run. Include the git SHA so results are reproducible.

### Template

```
### <date> — <git sha> — <milestone>
Models: STT=<id> LLM=<id> TTS=<id>
| Suite | Metric | Value | Threshold | Pass |
|---|---|---|---|---|
Notes: <what changed since the last run, failures investigated, dataset additions>
```

### 2026-09-11 — 8baa145 — `qwen2.5:7b` on Ollama, as the local fallback

Full record: `backend/evals/results/qwen2.5-7b-local-E1.json`. Run with
`make evals SUITE=E1,E4,E6 MODEL=qwen2.5:7b` plus `--provider ollama`.

| Suite | Metric | Value | Threshold | Pass |
|---|---|---|---|---|
| E1 Slide routing | accuracy | 57.5 % | ≥ 90 % | **no** |
| E1 Slide routing | false navigation | 8.3 % | ≤ 5 % | **no** |
| E4 Spoken style | pass rate | 77.5 % | ≥ 95 % | **no** |
| E6 Tool-call hygiene | invalid calls | 0 | 0 | yes |
| E6 Tool-call hygiene | off-topic navigation | 0.0 % | ≤ 5 % | yes |

All forty items answered, which is the first thing worth saying.

**It fails the thresholds, and it is still the right fallback.** Compare the shape of the failure
with `gpt-oss-120b`, which scored a nearly identical 58.3 %. That model failed by producing
*nothing*: seven of eight paraphrased questions returned no visible text and the listener heard
"Sorry, I lost that one". This one failed by answering the question correctly and leaving the deck
where it was. Thirteen of its seventeen misses are of the form `N → N`: the right answer, spoken,
on the wrong slide. For a primary model that is a failure. For a fallback whose alternative is
silence, it is a good trade.

**Why the keyword fallback did not rescue it.** The server scores an unnavigated answer against the
deck and moves it when one slide wins clearly (TR-062). It declined to here, and correctly: on
"how do you know when I have stopped talking" it picked slide 3 as the best match but with a
runner-up close behind, so it stood down. That conservatism is what keeps false navigation near
zero for the hosted model, and loosening a shared safety net to flatter a weaker one would trade a
real property for a measured number. It was left alone.

**Latency, measured through the real pipeline on an M4 Pro:**

| Question | Local TTFT | Local whole turn | Hosted TTFT |
|---|---|---|---|
| "How do you handle interruptions?" | 2,142 ms | 3,040 ms | 475 ms |
| "Tell me about the latency budget." | 3,132 ms | 4,092 ms | 475 ms |

Four to six times slower to first token, because a seven-billion-parameter model has to read the
whole ~2,900-token prompt before it says anything. The product says so on screen rather than
letting the listener guess.

**The one outlier worth naming:** a single answer ran to 26 sentences and 231 words. Most of the
style failures are six-to-eight-sentence answers against a five-sentence allowance; that one is a
different thing, and it is the argument for a hard cap in the chunker rather than a prompt asking
politely.

### 2026-09-11 — a07b87e — `openai/gpt-oss-120b`

Full record: `backend/evals/results/gpt-oss-120b-E1.json`.

| Suite | Metric | Value | Threshold | Pass |
|---|---|---|---|---|
| E1 Slide routing | accuracy | 58.3 % | ≥ 90 % | **no** |
| E1 Slide routing | false navigation | 0.0 % | ≤ 5 % | yes |
| E4 Spoken style | pass rate | 87.5 % | ≥ 95 % | **no** |
| E6 Tool-call hygiene | invalid calls | 2 | 0 | **no** |
| E6 Tool-call hygiene | off-topic navigation | 0.0 % | ≤ 5 % | yes |

24 of 40 items answered; 16 were excluded after the free tier refused them, and are not counted as
wrong answers.

**What the failures were.** Seven of the eight paraphrased questions that got an answer produced
the fallback apology, "Sorry, I lost that one. Could you ask me again?" — which is what the pipeline
says when the model's stream ends with no visible characters. This is the reasoning-model failure
mode: the token allowance is spent on hidden reasoning before any answer is emitted. Direct
questions, where the slide is named almost literally, mostly worked; paraphrases, where the model
has to think first, mostly did not. That is the wrong way round for this product, whose entire
premise is that you can ask in your own words.

The two invalid tool calls were both `upstream error: Failed to parse tool call arguments as JSON`,
raised by Groq's own parser rather than by this code. The three style failures were answers of
seven, eight and nine sentences, against a five-sentence allowance.

**Nothing here was wrong about navigation itself.** False navigation was zero: every off-topic
question was declined without moving the deck, and every "stay on this slide" question was answered
in place. When this model answers, it routes sensibly. The problem is how often it does not answer.

**Conclusion: the default stays `qwen/qwen3.8-27b`.** This run is the numeric version of the
qualitative comparison recorded below, and it agrees with it.

### 2026-09-11 — a07b87e — `qwen/qwen3.8-27b` — **not a valid run**

Full record: `backend/evals/results/qwen3.8-27b-release.json`.

Every one of the 90 items was refused by the free tier with HTTP 429, with retry-after values of
115 to 224 seconds, so nothing was measured: 0 of 40 routing items answered, 0 of 16 interruption
items gradeable, 0 of 31 groundedness items graded. The suite is recorded here rather than quietly
discarded, because a run that measured nothing is a fact about the day, not a fact about the agent,
and deleting it would leave the model comparison looking more complete than it is.

**Why.** The free tier allows 200,000 tokens per model per day, a turn through this pipeline
costs about 3,000 tokens per model call and two calls when it navigates, so E1 alone is about a
day's budget -- and by the time the suites were finished, the day's Qwen budget had already gone on
development, integration tests and live verification. The three items the judge did grade before
the budget ran out all agreed with their hand labels.

**What to do about it.** Run it when the budget resets:

```bash
make evals MODEL=qwen/qwen3.8-27b
```

The runner now waits out a rate limit for up to five minutes per attempt, which is enough to grind
through a throttled window; a constrained run takes a couple of hours of mostly waiting.

