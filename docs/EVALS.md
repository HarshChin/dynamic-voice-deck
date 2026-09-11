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
about 2,800 tokens (read off the 429 bodies), a turn that navigates makes two, and E2 and E3 add a
judge call each. The daily ceiling is not a midnight reset but a bucket of 200,000 tokens per model
that refills continuously at about 2.3 tokens a second: the rate-limit headers on a probe showed 72
requests used against a daily 1,000 with a reset of 1 h 43 m 40 s, which is 72 × 86.4 s exactly, and
the same arithmetic on tokens gives 2.31 a second. An empty bucket refuses a ~2,800-token call with
a wait of about twenty minutes, and the refused request appears to be charged like a served one:
the retry after the advertised wait was refused again, for the full wait again. At the original
sizes that put **E1 alone at roughly 190,000 tokens -- a whole day's budget for one model** -- and
the six suites at two and a half days; an earlier version of this paragraph said a third of a day,
an underestimate by a factor of seven. The sets were therefore cut on 2026-09-11 (*Sizing*, below):
E1 with E4 and E6 is now about 30 calls and 85,000 tokens, the whole run about 75 calls and
170,000. Run them at milestone boundaries, not on every commit.

**Pace the run; do not let it be refused.** The 429 body from the per-minute limiter reads
`input tokens per minute (ITPM): Limit 7000, Used 5406, Requested 2816` -- and that `Used` figure
was recorded one second after a run with *zero* successful calls was stopped. A refused request
counts against the very window it was refused for, so retrying on the advertised `retry-after`
re-fills that window and loops indefinitely; one such run made 34 attempts in ten minutes and
completed none. The runner therefore paces proactively, and by default: 31 seconds between model
calls -- subject and judge share one pacer, because they share the account's minute -- so that two
~2,800-token calls fit inside a 7,000-token minute and nothing is ever refused. Any retry that does
happen waits a full minute, since a shorter wait cannot succeed. `--concurrency 1` keeps items from
competing. `--max-wait` is the bound past which a wait is read as the day being spent rather than
the minute being full; raising it does not help, because the bucket refills at one call per twenty
minutes, so the runner stops instead (TR-205).

```bash
cd backend && GROQ_API_KEY="$(cat ~/.dvd-eval-key)" uv run python -m evals.run_evals \
  --suite E1,E4,E6 --model qwen/qwen3.8-27b --out evals/results/qwen3.8-27b-release.json
```

E1 at two calls a minute is about sixteen minutes, the whole run about forty. `--min-interval 0`
turns pacing off for a local model, which has no limiter to respect. Nothing in the run logs a
credential.

**Two measurement decisions worth stating.** An item the provider refused with a 429 is excluded
from the denominator rather than counted as a wrong answer: otherwise a run during a rate limit
measures the free tier instead of the agent, and the number moves for reasons that have nothing to
do with the code. And the runner waits out a per-minute limit (a full minute) but not a daily one:
the first wait past the bound marks the day as spent, no further item or judge call is attempted,
and each suite's note says how many items were never attempted, separately from how many the
provider refused (TR-205).

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

Categories, three items each:

| Category | Count | Example |
|---|---|---|
| direct | 3 | "How does barge-in work?" → 4 |
| paraphrase | 3 | "Why does it take a moment before you answer?" → 2 |
| relative | 3 | "Next slide please.", "Go back one.", "Take me back to the start." |
| cross-reference | 3 | "Which slide talks about cost?" → 6 |
| stay | 3 | "Can you expand on that second point?" → `null` (no navigation) |
| off-topic | 3 | "How much does a Groq subscription cost per month?" → `null` |

Items are added whenever a real session mis-routes; the utterance is copied verbatim from the event log.

### Sizing (2026-09-11)

The sets were 40, 16 and 31 items until release day, when two attempts to run E1 against the
default model both ran into the daily budget (results log, below). The arithmetic: a call through
the shipped prompt is about 2,800 tokens, a navigating turn makes two, the daily bucket holds
200,000 per model and refills at about 2.3 tokens a second. The forty-item routing set was 68 calls
and about 190,000 tokens -- the whole day -- and the six suites together two and a half days. A
gate that cannot run on the day it gates is not a gate.

| Set | Was | Is | Calls | What the threshold allows at this size |
|---|---|---|---|---|
| routing | 40 | 18, three per category | ~30 | 90 % accuracy: one miss |
| interruption | 16 | 6 | ~13 with the judge | 10 % repetition: none |
| grounded | 31 | 10, five unanswerable | ~20 with the judge | 80 % decline: one miss |
| judge calibration | 10 | 10 | 10 | 90 % agreement: one disagreement |

Whole run about 75 calls and 170,000 tokens; E1 with E4 and E6 about 30 calls and 85,000. The
items kept were chosen for spread: every target slide, every relative form (next, back, start),
the cross-references that need the notes rather than the titles, and the off-topic question about
Groq's subscription cost, which is the one that tempts a move to the trade-offs slide. Rows in the
model comparison measured on the old sets say so. Sets still grow by TR-203 when a real utterance
mis-routes, and a run's cost grows with them, knowingly.

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
| **qwen/qwen3.8-27b (Groq)** | **83.3 %** (18 of 18 answered) | **0.0 %** | 88.9 % | 0 | **current default.** Eighteen-item set, 2026-09-11, no refusals. Misses the routing bar by two items and the style bar by two answers; every stay and off-topic item was handled in place and no tool call was invalid. Also: E2 0 % repetition with one judged phantom reference, E3 2.00 / 2 and 100 % declined, judge calibration 10 of 10. The release run, below. |
| openai/gpt-oss-120b (Groq) | **58.3 %** (24 of 40 answered) | 0.0 % | 87.5 % | 2 | rejected. Forty-item set. Seven of eight paraphrased questions produced no visible answer at all. |
| openai/gpt-oss-20b (Groq) | — | — | — | — | untested; same reasoning-model family as the 120b. |
| **qwen2.5:7b (Ollama, local)** | **57.5 %** (40 of 40 answered) | 8.3 % | 77.5 % | 0 | **the fallback** (TR-085). Forty-item set. Not a candidate for primary; see the run below for why it is a good fallback anyway. |

The `gpt-oss-120b` and `qwen2.5:7b` rows were made on the forty-item routing set, before it was cut
to eighteen (*Sizing*, above); the default model's row is on the eighteen-item set. The categories
are the same and the smaller set is balanced across them, but a number from one set is not directly
comparable with a number from the other.

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

### 2026-09-11 — 435f074, working tree — `qwen/qwen3.8-27b` — **E5 re-run, unpaced**

Full record: `backend/evals/results/qwen3.8-27b-release-latency.json`. Three typed questions through
the real pipeline with real Kokoro synthesis, the model unpaced, and a quiet minute before each turn
so the per-minute allowance was clear. Stamped with the commit it started under; the E5 change it
exercises was uncommitted in that tree and lands in the commit that records this entry.

| Stage | Turn 1 | Turn 2 | Turn 3 | p50 | p95 | Threshold | Pass |
|---|---|---|---|---|---|---|---|
| model first token | 670 ms | 540 ms | 486 ms | 540 ms | 670 ms | ≤ 2,500 ms | yes |
| model whole answer | 3,046 ms | 2,256 ms | 2,098 ms | 2,256 ms | 3,046 ms | — | — |
| synthesis first chunk | 341 ms | 263 ms | 272 ms | 272 ms | 341 ms | ≤ 600 ms | yes |

Server-side first audio is first token plus first chunk: roughly 0.8 to 1.0 s here, inside the
1.5 s median budget of TRD §8.1 before endpointing and transcription are added on top. Against the
judged file's 26 to 59 seconds for the same stage, this is the measurement; that was the pacer.

### 2026-09-11 — c7d858e — `qwen/qwen3.8-27b` — **the release run**

Full records: `backend/evals/results/qwen3.8-27b-release.json` (E1, E4, E6; 19:20–19:35) and
`backend/evals/results/qwen3.8-27b-release-judged.json` (judge calibration, E2, E3, E5; 19:36–20:02),
both on a fresh account's daily budget, paced at one call per 31 seconds, with **no refusal of any
kind**. The judged file is stamped `435f074` because the runner read the SHA when it wrote the file
and that commit landed mid-run; the code that ran was `c7d858e`. The runner now reads the SHA when
it starts (TC-BE-351); the stamp is left as written, with this correction beside it.

| Suite | Metric | Value | Threshold | Pass |
|---|---|---|---|---|
| JUDGE | agreement with hand labels | 100 % (10 of 10) | ≥ 90 % | yes |
| E1 Slide routing | accuracy | 83.3 % (15 of 18) | ≥ 90 % | **no** |
| E1 Slide routing | false navigation | 0.0 % (0 of 6) | ≤ 5 % | yes |
| E2 Interruption memory | repetition | 0.0 % (0 of 6) | ≤ 10 % | yes |
| E2 Interruption memory | phantom reference | 16.7 % (1 of 6) | 0 % | **no** |
| E3 Groundedness | mean score, answerable | 2.00 / 2 (5 of 5 scored 2) | ≥ 1.70 | yes |
| E3 Groundedness | decline rate, unanswerable | 100 % (5 of 5) | ≥ 80 % | yes |
| E4 Spoken style | pass rate | 88.9 % (16 of 18) re-derived; 55.6 % as first recorded | ≥ 95 % | **no** |
| E5 Latency | synthesis first chunk, p95 | 426 ms | ≤ 600 ms | yes |
| E5 Latency | model first token, p95 | 670 ms, in the unpaced re-run above | ≤ 2,500 ms | yes |
| E6 Tool-call hygiene | invalid calls | 0 | 0 | yes |
| E6 Tool-call hygiene | off-topic navigation | 0.0 % (0 of 3) | ≤ 5 % | yes |

**Routing: three misses, three different causes.** By category: direct 2 of 3, paraphrase 2 of 3,
relative 2 of 3; cross-reference, stay and off-topic 3 of 3 each.

- `r020`, "Next slide please." from slide 2, landed on **5**. The model called `go_to_slide` on all
  three round trips the turn allows: after each move the tool result showed a new current slide, the
  instruction "next" still stood, and it moved again. A relative command has to be resolved once.
  This is the run's one real routing defect, and a follow-up rather than a fix tonight: the tool
  loop's own comment records three cheaper variants of that exchange each failing against the live
  API, so the change needs a live test the budget does not have left.
- `r010`, "Why does it take a moment before you answer?", answered the question -- correctly, and
  about slide 2 -- while leaving the deck on slide 1, saying "the next slide breaks it down" instead
  of going there. The keyword fallback stood down, as designed, because the answer named several
  stages and no slide won clearly.
- `r004`, "What are the trade-offs?", got back the provider's own failure string, "assistant turn
  failed before producing text", and nothing else: no tool call, no words. That string exists
  nowhere in this repository.

**Style: the instrument was wrong first.** As recorded, 8 of 18 answers failed, six of them for
"too many sentences". E4 was counting the chunker's output, and the chunker splits a long sentence
at a clause so that speech can start early: a three-sentence, 46-word answer arrived as six pieces.
Fixed in `435f074`. Re-derived from the same recorded answers, E4 is 88.9 %, with two real failures:
a 99-word answer to "Go back one.", and the `r010` answer, which arrived wrapped as
`assistant turn 2 {"content":"..."}` and now counts as markup. To reproduce the re-derivation from
the record:

```bash
cd backend && uv run python -c '
import json; from evals.suites import SuiteResult, e4_style
d = json.load(open("evals/results/qwen3.8-27b-release.json"))
e1 = next(s for s in d["suites"] if s["suite"] == "E1")
r = e4_style([SuiteResult(suite="E1", title="Slide routing", items=e1["items"])])
print(r.metrics, [(i["id"], i["problems"]) for i in r.items if not i["ok"]])'
```

**The model leaks its own chat template.** Across the two files, four answers opened with labels
that are not in this codebase: "assistant reasoning", `assistant turn 2 {"content":"`, and twice
the failure string above -- once as the whole answer (`r004`) and once, on E3's slide-6 question,
followed by a perfect answer. Since `435f074` the stripper that removes our own two markers
(TR-088) removes these too, so a listener never hears them; the eval keeps the model's raw text, so
the record still shows them.

**Interruption memory: one strict verdict.** No answer repeated what had been heard. One was judged
a phantom reference: asked "So why did you not use it?" after being cut off on slide 6, the agent
said "This pipeline owns every one of those milliseconds", which restates an unheard sentence
("This pipeline owns every millisecond instead") without claiming to have said it. The rubric asks
whether the reply refers to unheard content *as though it had been said*; the judge read a
restatement as a reference. At a 0 % threshold on six items, one such verdict fails the suite. The
verdict stands as recorded. The rubric's wording is a follow-up, and so is the question underneath
it: restating a true fact from the notes that the listener did not hear is arguably the right thing
to do.

**Groundedness is the clean result.** Every answerable question scored 2 of 2, and every
unanswerable one was declined with the deck's own formula -- "That's not in this deck. What I can
tell you is..." -- including the two that invite a confident guess, the model's parameter count and
the Silero version. The judge agreed with all ten hand labels first, so these numbers are believed.

**Latency, and the fourth instrument fault.** Synthesis first chunk 292–426 ms across three turns,
p95 426 ms against a 600 ms bar. The model's time to first token as recorded in the judged file --
26.5 s, 29.9 s, 58.8 s -- is not the model: it is the pacer's wait, which happens inside the
provider's `stream`, after the turn has started its clock. The suite now runs unpaced and lets a
minute pass before each turn so its calls fit the per-minute allowance on their own (TC-BE-352).
The re-run is the entry above this one.

**What this changes.** The default stays `qwen/qwen3.8-27b`. It answered every item, navigated
correctly on 15 of 18 with zero false moves, declined everything it should, and its failures are
specific and fixable. Against the two alternatives measured earlier on the larger set it is not
close: `gpt-oss-120b` produced no visible answer on seven of eight paraphrases, and the local
fallback answered well but left the deck where it was on thirteen items. The release bar is not met
on E1, E2 and E4, and the README says so.

### 2026-09-11 — a8f1e88 — `qwen/qwen3.8-27b` — attempt 2, paced: **no record, and the reason the suites shrank**

No result file: the runner wrote results only at the end of a run, and this run did not end.

Run on a second account's fresh daily budget, with the pacer that the morning's loop had made
necessary (`--min-interval 31 --concurrency 1 --max-wait 1800`), E1, E4 and E6 on the forty-item
routing set. For 24 minutes it did exactly what it should: 45 calls, one every 31 seconds, **zero
per-minute refusals** -- the reactive runner had managed no successful call at all in ten minutes
-- and a single `turn.empty_answer` warning across the 28 or so items it got through. Then at 18:15
the daily bucket refused a call with a wait of 855 s. The retry, after that wait, was refused with
1,217 s: the full refill of one call again, as though the refused request had been charged. A
one-token probe of the same key at 18:28 succeeded and returned the headers that gave the refill
rate. The run was killed at 18:40 with its 45 answers in memory and nowhere else.

**What it establishes.** The pacer works: the per-minute limiter was never tripped. The daily
budget is a continuously refilling bucket, and at 200,000 tokens a day and ~2,800 tokens a call the
forty-item E1 could never be run on the day it was meant to gate, on either key, alongside the
development that draws on the same keys. So the suites were resized (*Sizing*, above); the runner
now stops attempting items on the first daily refusal instead of crawling, and says in each suite's
note how many it never attempted (TR-205); and pacing is its default rather than a flag.

**What it does not establish.** Nothing about the agent: 45 calls whose results were never written
are not a measurement. The default model's row above stayed *pending* until the eighteen-item run
completed on a fresh bucket later the same evening -- the release-run entry above this one.

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

### 2026-09-11 — a07b87e — `qwen/qwen3.8-27b` — attempt 1, **not a valid run**

Full record: `backend/evals/results/qwen3.8-27b-release-attempt-1.json`.

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

**What to do about it.** It was tried again the same evening on a second account's key -- the
attempt-2 entry at the top of this log -- and that attempt is why the suites are now the size they
are.

