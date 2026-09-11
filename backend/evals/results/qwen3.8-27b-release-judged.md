### 2026-09-11 — 435f074 — qwen/qwen3.8-27b

Models: LLM=`qwen/qwen3.8-27b` judge=`qwen/qwen3.8-27b`

| Suite | Metric | Value | Threshold | Pass |
|---|---|---|---|---|
| JUDGE Judge calibration | agreement | 100.0 % | >= 90.0 % | yes |
| E2 Interruption memory | repetition | 0.0 % | <= 10.0 % | yes |
| E2 Interruption memory | phantom_reference | 16.7 % | <= 0.0 % | **no** |
| E3 Groundedness | mean_score | 2.00 / 2 | >= 1.70 / 2 | yes |
| E3 Groundedness | decline_rate | 100.0 % | >= 80.0 % | yes |
| E5 Latency | llm_ttft_ms_p50 | 29853 ms | — | — |
| E5 Latency | llm_ttft_ms_p95 | 58839 ms | <= 2500 ms | **no** |
| E5 Latency | llm_total_ms_p50 | 59456 ms | — | — |
| E5 Latency | llm_total_ms_p95 | 60002 ms | — | — |
| E5 Latency | tts_ttfb_ms_p50 | 309 ms | — | — |
| E5 Latency | tts_ttfb_ms_p95 | 426 ms | <= 600 ms | yes |

Notes: JUDGE: Judged suites are only believed when this clears its threshold. E2: 6 of 6 items were gradeable. E3: 5 answerable and 5 unanswerable items graded. E5: 3 live turns with real synthesis; p95 of three runs is the maximum.
