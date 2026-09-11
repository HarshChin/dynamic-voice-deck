### 2026-09-11 — 8baa145 — qwen2.5:7b

Models: LLM=`qwen2.5:7b` judge=`qwen2.5:7b`

| Suite | Metric | Value | Threshold | Pass |
|---|---|---|---|---|
| E1 Slide routing | accuracy | 57.5 % | >= 90.0 % | **no** |
| E1 Slide routing | false_navigation | 8.3 % | <= 5.0 % | **no** |
| E4 Spoken style | pass_rate | 77.5 % | >= 95.0 % | **no** |
| E6 Tool-call hygiene | invalid_calls | 0 | <= 0 | yes |
| E6 Tool-call hygiene | off_topic_navigation | 0.0 % | <= 5.0 % | yes |

Notes: E1: 40 of 40 items answered. E4: Derived from 40 answers across the suites that ran. E6: Derived from the E1 traces; a rejected tool call surfaces as a turn error.
