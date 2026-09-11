### 2026-09-11 — a07b87e — openai/gpt-oss-120b

Models: LLM=`openai/gpt-oss-120b` judge=`openai/gpt-oss-120b`

| Suite | Metric | Value | Threshold | Pass |
|---|---|---|---|---|
| E1 Slide routing | accuracy | 58.3 % | >= 90.0 % | **no** |
| E1 Slide routing | false_navigation | 0.0 % | <= 5.0 % | yes |
| E4 Spoken style | pass_rate | 87.5 % | >= 95.0 % | **no** |
| E6 Tool-call hygiene | invalid_calls | 2 | <= 0 | **no** |
| E6 Tool-call hygiene | off_topic_navigation | 0.0 % | <= 5.0 % | yes |

Notes: E1: 24 of 40 items answered; 16 excluded after a provider failure. E4: Derived from 24 answers across the suites that ran. E6: Derived from the E1 traces; a rejected tool call surfaces as a turn error.
