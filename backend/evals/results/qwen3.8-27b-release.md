### 2026-09-11 — c7d858e — qwen/qwen3.8-27b

Models: LLM=`qwen/qwen3.8-27b` judge=`qwen/qwen3.8-27b`

| Suite | Metric | Value | Threshold | Pass |
|---|---|---|---|---|
| E1 Slide routing | accuracy | 83.3 % | >= 90.0 % | **no** |
| E1 Slide routing | false_navigation | 0.0 % | <= 5.0 % | yes |
| E4 Spoken style | pass_rate | 55.6 % | >= 95.0 % | **no** |
| E6 Tool-call hygiene | invalid_calls | 0 | <= 0 | yes |
| E6 Tool-call hygiene | off_topic_navigation | 0.0 % | <= 5.0 % | yes |

Notes: E1: 18 of 18 items answered. E4: Derived from 18 answers across the suites that ran. E6: Derived from the E1 traces; a rejected tool call surfaces as a turn error.
