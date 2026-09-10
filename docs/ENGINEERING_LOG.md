# Engineering Log

A dated record of every meaningful change to the codebase and the reasoning behind it. Where the git history says *what* changed, this log says *why*, what alternatives were considered, and what was learned. One entry per change set; newest at the top of each day.

Entry template:

```
### <YYYY-MM-DD> · <short title> · <commit sha or "uncommitted">
**Scope:** files or modules touched
**Change:** what was done, in two or three sentences
**Why:** the problem or requirement (link PRD feature / TR id / TC id)
**Alternatives considered:** what else was on the table and why it lost
**Verification:** tests added or run, eval impact, measurements
**Follow-ups:** open items created by this change
```

Rules:

- Write the entry in the same commit as the change.
- Record dead ends too. A reverted approach with its reason is more useful than a clean history.
- When a measurement drives a decision (latency, accuracy), put the number here.
- Keep entries factual; opinions go in "Alternatives considered".

---

## 2026-09-09

### 2026-09-09 · Project inception and documentation set · uncommitted
**Scope:** `docs/PRD.md`, `docs/TRD.md`, `docs/TEST_CASES.md`, `docs/EVALS.md`, `docs/ENGINEERING_LOG.md`, `CLAUDE.md`, `.gitignore`, `.env.example`
**Change:** Wrote the product requirements, technical requirements with full architecture, the test-case catalogue seeded with ~90 planned cases, the evaluation design, and this log. Set engineering standards in `CLAUDE.md`.
**Why:** Establish shared, reviewable intent before code. The system has several concurrent moving parts (VAD, cancellable async pipeline, gapless playback) where an unclear contract would be expensive to fix later.
**Alternatives considered:**
- *Speech-to-speech API (Gemini Live, OpenAI Realtime)* — faster to build, native barge-in, but turn-taking logic would be opaque and vendor-owned; Gemini Live is free but closed. Rejected in favour of an open-weight STT→LLM→TTS pipeline where every stage is inspectable and swappable.
- *Continuous audio streaming to the backend with server-side VAD* — lower theoretical latency (STT could start before the utterance ends) but multiplies STT requests, complicates cancellation, and adds a network hop to barge-in detection. Rejected for v0.1.0; browser VAD with utterance-level STT chosen. Revisit if `first_audio_ms` misses budget.
- *Next.js API routes as the backend* — single repo simplicity, but Python's async model and audio ecosystem fit the pipeline better. FastAPI chosen.
- *Kokoro via PyTorch package vs `kokoro-onnx`* — ONNX chosen for smaller dependency surface and CPU speed; the PyTorch package is the fallback if wheels fail.
**Verification:** Documents cross-reference each other by ID (F-, TR-, TC-, E-). No code yet.
**Follow-ups:** Verify `kokoro-onnx` installs on Python 3.12 arm64 at the start of M2. Decide default Kokoro voice by ear. Confirm Groq SSE tool-call delta format with a recorded fixture before writing the parser.
