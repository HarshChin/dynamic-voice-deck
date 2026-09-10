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

## 2026-09-10

### 2026-09-10 · Phase 0: scaffolding, quality gates, and the review that paid for itself · uncommitted
**Scope:** `backend/` (pyproject, app/{config,errors,logging_setup,main}.py, tests/), `frontend/`
(Vite app, ESLint/Prettier/Vitest config, placeholder UI), `Makefile`, `.github/workflows/ci.yml`,
`.editorconfig`, `.gitignore`, `.env.example`, `README.md`, `docs/{PRD,TRD,TEST_CASES}.md`, `CLAUDE.md`
**Change:** Stood up the repository: uv-managed Python 3.12 backend with settings, an error hierarchy,
structlog with a standard-library bridge, and a health endpoint; a Vite/React frontend with a
placeholder page that probes the backend; Make targets, CI, and the first 112 tests. No product
behaviour yet, by design.
**Why:** Phase 0's job is to make every later phase cheap. A wrong contract in settings, logging, or
the async lint rules would propagate into the pipeline code where it is expensive to unpick.

**Alternatives considered:**
- *Keep the Vite template's `oxlint`* — rejected in favour of `typescript-eslint` with type-checked
  rules. `no-floating-promises`, `no-misused-promises`, and `await-thenable` guard the audio teardown
  and WebSocket code that this project lives or dies on, and `oxlint` has no type-aware equivalent.
  Cost: a slower lint. Worth it.
- *Install the backend as a package* — rejected. It is an application, not a library, so
  `[tool.uv] package = false` plus `pythonpath = ["."]` avoids a build backend entirely.
- *Document constructor arguments on `__init__`* — rejected. Google style puts them on the class, so
  `D107` is ignored rather than duplicating `Args:` in two places where they would drift.
- *`filterwarnings = ["error"]` in pytest* — deferred. Several dependencies emit import-time
  `DeprecationWarning`s on 3.12 that would fail collection before any test runs. Revisit with a
  targeted ignore list once CI exercises the full set.

**Verification:** `make lint` (ruff, ruff format, mypy --strict, ESLint, tsc, Prettier) and
`make test` both green. 106 backend tests at **100% line coverage** on every backend module, plus 6
frontend tests. Test isolation confirmed by running the suite in three orderings and each file alone.
Both servers start; the health endpoint answers; the Vite proxy reaches the backend.

**Smoke test found a real bug.** An unrelated PHP server on the development machine holds IPv6 port
8000. macOS resolves `localhost` to `::1` first and Node 17+ no longer reorders DNS results, so the
Vite proxy aimed at `localhost:8000` was silently reaching *that* server and returning HTML where the
app expected JSON. Pinned the proxy to the IPv4 literal with the reasoning recorded inline, since
this looks like needless specificity to anyone who has not hit it.

**Review.** Five independent lenses (backend Python, test quality, build/CI, frontend tooling, spec
drift) raised 39 findings; each was handed to a skeptic instructed to refute it. 19 survived and were
fixed; 20 were refuted, mostly for overstated impact rather than wrong observation. Findings worth
recording:
- `_load_settings` caught only `ValidationError`, but pydantic-settings raises `SettingsError` from
  inside the settings *source* when it cannot JSON-decode a complex field. A `CORS_ORIGINS` written in
  the format `.env.example` itself documented therefore crashed with a raw traceback instead of the
  actionable `ConfigError`. Fixed both the wrapper and the misleading documentation.
- `GROQ_API_KEY=` (exactly what `cp .env.example .env` leaves) parsed as `SecretStr("")`, so an
  `is None` fail-fast check would not fire and the failure would surface later as an opaque `401`.
  Now normalised to `None`.
- `LOG_LEVEL=NOTSET` was accepted. On the *root* logger NOTSET means level 0, which emits everything —
  more verbose than DEBUG, and enough to switch on the DEBUG transcript logging TR-181 gates. Removed
  from the allowed set, and its test moved from the accepted list to the rejected one.
- No coverage artefact was git-ignored, so the next `git add -A` would have committed `.coverage`, a
  binary SQLite file rewritten on every test run.
- `npm run typecheck` compiled only `tsconfig.app.json`, so `vite.config.ts` was type-checked by
  nothing. Switched to `tsc -b --noEmit` and proved the fix by injecting a type error and watching it
  fail.
- CI ran `uv sync` without `--frozen` while the frontend used `npm ci`, so only half the build honoured
  its lockfile. The frontend lockfile was also stale, still carrying `oxlint` and 20 platform binaries.
- The env-scrubbing test fixture deleted only the lower and upper spellings of each setting, but
  `case_sensitive=False` matches any casing, so a shell exporting `Groq_Api_Key` would have defeated
  the isolation. Now scans the real environment and deletes anything that case-folds to a field name.

**Two findings the skeptics refuted but I acted on anyway.** A verifier demonstrated that pointing the
frontend at `https://evil.example.com` left all three tests passing, then refuted the finding on the
grounds that the scenario could not occur in production. The mutation survival is the point: Phase 0
had just been bitten by exactly that bug class in the proxy. Rewrote the frontend tests and confirmed
by mutation that a wrong URL now fails. Likewise the "stale lockfile" refutation was right that both
install paths agree today, but a lockfile listing a removed dependency is still wrong, so it was
refreshed.

**Documentation corrected against the code:** the TRD's health-body contract was missing the `version`
field that the code, README, and tests all have; `logging_setup.py` existed in no document; CLAUDE.md
still said React 18 and described a plain-`logging` standard the implementation had already
outgrown; and every weekday label in the PRD milestone table was one day early (9 September 2026 is a
Wednesday).

**Follow-ups:**
- `eslint.config.js` lints `**/*.js` with Node globals, which will flag `AudioWorkletProcessor` when
  the capture worklet lands in Phase 2. Add a worklet-specific config block then.
- Enable `fail_under` for coverage once the pipeline modules exist (TRD §12.3).
- Revisit `filterwarnings = ["error"]` with a targeted ignore list.
- Confirm Kokoro's returned sample rate is 24,000 when weights download in Phase 2.

### 2026-09-10 · Toolchain probes close the two biggest unknowns · uncommitted
**Scope:** `docs/TRD.md` (TR-010, TR-083, TR-086, §2.2, §3.2, §16)
**Change:** Probed `kokoro-onnx` on Python 3.12 arm64 in a throwaway venv before committing to it, and
scaffolded the frontend to learn the template's real dependency versions. Folded both results into the TRD.
**Why:** TRD §16 listed Kokoro wheel availability as the top technical risk, due in M2. Finding out on
Wednesday that TTS does not install would have cost a day. The frontend probe was free (the scaffold had
to happen anyway) and changed a documented decision.
**Findings:**
- `kokoro-onnx` 0.6.1 installs cleanly with `onnxruntime` 1.29.0 and `numpy` 2.5.3. Import succeeds and
  `onnxruntime` reports a **CoreML** execution provider alongside CPU, so synthesis can use the Apple
  Silicon neural engine. Phonemisation ships via `espeakng-loader`, so no system `espeak` install is needed.
- **API correction.** `Kokoro.create(text, voice, speed, lang, ...)` returns `(float32 array, sample_rate)`
  for the whole utterance. It is not an incremental generator, so TR-083's claim that the first frame
  leaves before the sentence finishes synthesising was wrong. Rewrote TR-083 and added TR-086: time to
  first audio now depends on the `SentenceChunker` splitting early, which puts TR-042 on the critical path.
- The current Vite template ships React 19, TypeScript 6, Vite 8, and `oxlint` rather than the React 18 and
  ESLint the TRD assumed. Updated §2.2 and §3.2.
**Alternatives considered:**
- *Keep `oxlint`* (the template default, much faster) — rejected. It has no type-aware rules, and
  `no-floating-promises` / `no-misused-promises` / `await-thenable` guard exactly the code most likely to
  break here: playback teardown, `AudioContext` lifecycle, WebSocket handlers. One linter, chosen for the
  rules that matter, beats two linters or a fast one that misses the bug class.
- *Run both linters* — rejected as friction for no coverage gain.
- *Pin TypeScript to 5.9 for a supported `typescript-eslint` range* — deferred. TS 6 may emit an
  unsupported-version warning; if it hard-fails, pinning is the fallback and is a one-line change.
**Verification:** Probe venv imported `kokoro_onnx`, `onnxruntime`, `numpy`, `soundfile` and printed the
real `create()` signature. Frontend dependency install completed and versions were read back from
`package.json`.
**Follow-ups:** Confirm Kokoro's returned sample rate is 24,000 when weights are downloaded in Phase 2.
Confirm `typescript-eslint` 8.70 runs against TypeScript 6.0 during the Phase 0 smoke test.

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
