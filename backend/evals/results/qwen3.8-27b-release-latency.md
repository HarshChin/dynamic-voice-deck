### 2026-09-11 — 435f074 — qwen/qwen3.8-27b

Models: LLM=`qwen/qwen3.8-27b` judge=`qwen/qwen3.8-27b`

| Suite | Metric | Value | Threshold | Pass |
|---|---|---|---|---|
| E5 Latency | llm_ttft_ms_p50 | 540 ms | — | — |
| E5 Latency | llm_ttft_ms_p95 | 670 ms | <= 2500 ms | yes |
| E5 Latency | llm_total_ms_p50 | 2256 ms | — | — |
| E5 Latency | llm_total_ms_p95 | 3046 ms | — | — |
| E5 Latency | tts_ttfb_ms_p50 | 272 ms | — | — |
| E5 Latency | tts_ttfb_ms_p95 | 341 ms | <= 600 ms | yes |

Notes: E5: 3 live turns with real synthesis; p95 of three runs is the maximum.
