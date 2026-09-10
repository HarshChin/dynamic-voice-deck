# Agent Evaluations

Evals measure the **behaviour of the agent with real models**, which unit tests cannot cover because model output is non-deterministic. Suite definitions live in `docs/TRD.md` §13; datasets and the runner live in `backend/evals/`. This file records the design decisions behind each suite and the results of every recorded run.

## How to run

```bash
make evals                       # all suites, default model, writes backend/evals/results/<ts>.json + .md
make evals SUITE=E1 MODEL=llama-3.3-70b-versatile
```

Evals consume Groq free-tier quota (≈ 40–120 LLM calls for a full run). Run them at milestone boundaries, not on every commit.

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

The judge is the same LLM provider at `temperature=0` with rubric prompts in `backend/evals/judges/`. Judge outputs are strict JSON. A 10-item hand-labelled calibration set (`datasets/judge_calibration.jsonl`) is scored on every run; judge agreement with hand labels must be ≥ 90 % for the run to be valid.

## Model comparison

| Model | E1 accuracy | E1 false nav | E4 style | E6 invalid | Notes |
|---|---|---|---|---|---|
| openai/gpt-oss-120b (Groq) | — | — | — | — | default candidate |
| llama-3.3-70b-versatile (Groq) | — | — | — | — | fallback candidate |
| local (Ollama, TBD) | — | — | — | — | offline reference |

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

_No runs recorded yet._
