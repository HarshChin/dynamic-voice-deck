### 2026-09-11 — a07b87e — qwen/qwen3.8-27b

Models: LLM=`qwen/qwen3.8-27b` judge=`qwen/qwen3.8-27b`

| Suite | Metric | Value | Threshold | Pass |
|---|---|---|---|---|
| JUDGE Judge calibration | agreement | 30.0 % | >= 90.0 % | **no** |
| E1 Slide routing | accuracy | 0.0 % | >= 90.0 % | **no** |
| E1 Slide routing | false_navigation | 0.0 % | <= 5.0 % | yes |
| E2 Interruption memory | repetition | 0.0 % | <= 10.0 % | yes |
| E2 Interruption memory | phantom_reference | 0.0 % | <= 0.0 % | yes |
| E3 Groundedness | mean_score | 0.00 / 2 | >= 1.70 / 2 | **no** |
| E3 Groundedness | decline_rate | 0.0 % | >= 80.0 % | **no** |
| E4 Spoken style | pass_rate | 0.0 % | >= 95.0 % | **no** |
| E6 Tool-call hygiene | invalid_calls | 0 | <= 0 | yes |
| E6 Tool-call hygiene | off_topic_navigation | 0.0 % | <= 5.0 % | yes |

Notes: JUDGE: Judged suites are only believed when this clears its threshold. E1: 0 of 40 items answered; 40 excluded after a provider failure. E2: 0 of 16 items were gradeable. E3: 0 answerable and 0 unanswerable items graded. E4: Derived from 0 answers across the suites that ran. E5: 3 live turns with real synthesis; p95 of three runs is the maximum. E6: Derived from the E1 traces; a rejected tool call surfaces as a turn error.
