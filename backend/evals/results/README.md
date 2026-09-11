# Recorded eval runs

One JSON and one Markdown file per run. The JSON is the record: every item, every answer, every
judge rationale, so a number that looks wrong can be traced to the answer that produced it. The
Markdown is the summary table, which is pasted into `docs/EVALS.md` with the git SHA.

Only runs worth citing are kept here. Smoke runs made with `--limit` to prove a change to the
runner are deleted afterwards: they measure four items and would read like results.

| File | What it is |
|---|---|
| `gpt-oss-120b-E1.json` | E1, E4 and E6 against `openai/gpt-oss-120b`. 24 of 40 items answered. |
| `qwen3.8-27b-release-attempt-1.json` | The first attempted release run against the default model. Every item was refused by the free tier; kept because a run that measured nothing is a fact about the day. |
| `qwen3.8-27b-release.json` | The release run's E1, E4 and E6 against the default model on the eighteen-item set: 18 of 18 answered, no refusals. Its E4 row reads 55.6 % because of the chunk-counting fault fixed in `435f074`; re-derived from the same answers, 88.9 %. |
| `qwen3.8-27b-release-judged.json` | The release run's judge calibration, E2, E3 and E5. Stamped `435f074` but run on `c7d858e`: the SHA was read at write time, since fixed. Its E5 model latencies are the pacer's wait, not the model's; the re-run is `-latency.json`. |
| `qwen3.8-27b-release-latency.json` | E5 re-run for the release: three typed turns with real synthesis, the model unpaced, a quiet minute before each. First token p95 670 ms, first chunk p95 341 ms. This is the latency record; the judged file's E5 rows are superseded. |
