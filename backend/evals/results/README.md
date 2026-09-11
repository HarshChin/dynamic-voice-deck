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
