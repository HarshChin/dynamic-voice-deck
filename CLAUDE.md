# CLAUDE.md — Dynamic Voice Deck

Engineering guide for anyone (human or AI) working in this repository. This file is the source of truth for **how** we build. The documents below are the source of truth for **what** we build:

| Document | Purpose | When to update |
|---|---|---|
| `docs/PRD.md` | Product behaviour, features F1–F14, acceptance criteria | When behaviour or scope changes |
| `docs/TRD.md` | Architecture, component contracts, protocol, requirements TR-xxx, testing and eval strategy | When a technical decision or contract changes |
| `docs/TEST_CASES.md` | Living catalogue of test cases TC-xxx with status | **Every commit** that changes behaviour |
| `docs/EVALS.md` | Eval suite design and recorded results | Every milestone eval run |
| `docs/ENGINEERING_LOG.md` | Dated record of every change and the reasoning | **Every commit** |

## 1. What this project is

Dynamic Voice Deck is a voice-first slide presenter. An open-weight STT → LLM → TTS pipeline lets a user talk to a presenter agent that navigates a six-slide deck by tool calls and can be interrupted mid-sentence.

- **Backend:** Python 3.12, FastAPI, asyncio, WebSockets. Providers: Groq Whisper (STT), Groq `gpt-oss-120b` (LLM), Kokoro-82M via `kokoro-onnx` (TTS).
- **Frontend:** React 19, Vite 8, TypeScript 6 (strict). Silero VAD runs in the browser.
- **Distribution:** public GitHub repository that runs locally. No cloud deployment in v0.1.0.
- **Release target:** v0.1.0 on Friday 11 September 2026.

## 2. Quick commands

```bash
make setup      # uv sync (backend) + npm install (frontend)
make backend    # uvicorn app.main:app --reload --port 8000  (cwd backend/)
make frontend   # vite dev server on :5173                     (cwd frontend/)
make lint       # ruff check + ruff format --check + eslint + tsc --noEmit
make format     # ruff format + ruff check --fix + prettier
make test       # pytest unit + contract tests; coverage report
make test-integration   # real Groq/Kokoro; needs GROQ_API_KEY
make test-e2e   # Playwright against a fake-provider backend
make evals      # agent evals (TRD §13); consumes free-tier quota
```

Environment variables live in `.env` at the repo root (git-ignored). `.env.example` lists every variable with a comment. Never print, log, or commit secrets.

## 3. Working agreements

1. **PRD and TRD drive scope.** Implement features in milestone order (PRD §12, TRD §15). If a requirement is wrong or infeasible, edit the document in the same change and record the reasoning in `docs/ENGINEERING_LOG.md`.
1a. **Tests are written with the feature, not after.** Before implementing a feature, find or add its TC-xxx rows in `docs/TEST_CASES.md`; implement the tests alongside the code; update the status column in the same commit.
1b. **Log every change.** Each commit gets an entry in `docs/ENGINEERING_LOG.md` using the template there: scope, change, why, alternatives considered, verification, follow-ups. Dead ends are logged too.
2. **Always-runnable main.** Every commit on `main` starts and serves the deck. Feature work happens on short-lived branches merged when green.
3. **Small, reviewable changes.** One concern per commit. Conventional Commits: `feat(backend): ...`, `fix(frontend): ...`, `docs: ...`, `test: ...`, `chore: ...`, `refactor: ...`.
4. **Measure, do not guess.** Latency claims in the README must come from the metrics pipeline, not intuition.
5. **No vendor code outside `providers/`.** Groq, Kokoro, Ollama imports are allowed only in `backend/app/providers/`. Everything else depends on the `STTProvider` / `LLMProvider` / `TTSProvider` protocols in `providers/base.py`.
6. **Protocol is defined once.** WebSocket message types live in `backend/app/protocol.py` (Pydantic) and `frontend/src/protocol.ts`. Change both in the same commit; `tests/test_protocol_parity.py` enforces the match.
7. **Audio never touches disk.** Utterances and synthesized audio are held in memory only.

## 4. Python standards (backend)

### 4.1 Style

- **PEP 8** throughout, enforced by **ruff** (lint and format). Line length **100**.
- **Type hints on every function signature**, including return types and `-> None`. Use modern syntax: `list[str]`, `str | None`, `collections.abc.AsyncIterator`.
- **Google-style docstrings** on every public module, class, function, and method. Private helpers (`_name`) need a docstring only if the intent is not obvious from the name and types.
- f-strings for formatting. No `%` or `.format()`.
- `pathlib.Path` over `os.path`.
- Prefer dataclasses or Pydantic models over dicts for structured data crossing a function boundary.
- Enums (`StrEnum`) for closed sets of values such as session states and message types.
- Constants in `UPPER_SNAKE_CASE` at module top; no magic numbers inline (name the 600 ms endpoint timeout).

### 4.2 Docstring template

```python
async def transcribe(self, pcm16: bytes, sample_rate: int = 16_000) -> Transcript:
    """Transcribe a single finished utterance.

    Args:
        pcm16: Little-endian 16-bit mono PCM samples.
        sample_rate: Sample rate of ``pcm16`` in hertz.

    Returns:
        The transcript with text and provider latency in milliseconds.

    Raises:
        ProviderError: If the upstream service fails after one retry.
    """
```

Sections in this order when applicable: summary line (imperative, one line, ends with a period), blank line, extended description, `Args:`, `Returns:` / `Yields:`, `Raises:`, `Example:`.

### 4.3 Ruff configuration (in `backend/pyproject.toml`)

```toml
[tool.ruff]
line-length = 100
target-version = "py312"
src = ["app", "tests"]

[tool.ruff.lint]
select = [
  "E", "W",     # pycodestyle
  "F",          # pyflakes
  "I",          # isort
  "N",          # pep8-naming
  "D",          # pydocstyle
  "UP",         # pyupgrade
  "B",          # bugbear
  "ASYNC",      # async pitfalls
  "SIM",        # simplify
  "RUF",        # ruff-specific
  "PL",         # pylint subset
  "ARG",        # unused arguments
  "PTH",        # use pathlib
  "T20",        # no print()
  "S",          # bandit (security)
]
ignore = [
  "D203", "D213",   # conflict with D211/D212 (Google style)
  "D107",           # __init__ docstring: Google style documents constructor args
                    # in the class docstring instead, and duplicating them invites drift
  "PLR0913",        # too many arguments — provider constructors legitimately take config
]

[tool.ruff.lint.pydocstyle]
convention = "google"

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["D", "S101", "PLR2004", "ARG"]

[tool.ruff.format]
quote-style = "double"
docstring-code-format = true
```

Run `ruff check --fix` and `ruff format` before every commit. CI-equivalent locally: `make lint` must be clean.

### 4.4 Async and cancellation

Barge-in is implemented with `asyncio` cancellation, so these rules matter:

- One pipeline `asyncio.Task` per session. Store it on the `Session`; cancel and `await` it (swallowing `CancelledError`) before starting another.
- Never `except Exception` around an `await` without re-raising `asyncio.CancelledError`. Prefer `except (ProviderError, httpx.HTTPError)` with explicit types.
- Provider streams must be async generators or return `AsyncIterator`s so cancellation propagates into the HTTP stream.
- Blocking CPU work (Kokoro synthesis, WAV encoding) runs in `asyncio.to_thread` or a dedicated `ThreadPoolExecutor`. Never block the event loop for more than a few milliseconds.
- Use `asyncio.timeout()` for the 20 s pipeline watchdog.
- Bounded `asyncio.Queue(maxsize=2)` between chunker → TTS → socket so cancellation is fast and memory is flat.

### 4.5 Logging and errors

- `structlog` via `app/logging_setup.py`: module-level `logger = get_logger(__name__)`. Log events as a short dotted name plus key-value pairs, e.g. `logger.info("tts.first_audio", turn_id=n, ms=123)`, never a preformatted sentence. Standard-library records are bridged into the same chain, so third-party logs match. No `print`.
- Log every state transition, tool call, and provider latency at `INFO`; payload contents at `DEBUG`; never log audio bytes or API keys.
- Custom exception hierarchy in `app/errors.py`: `AppError` → `ProviderError`, `ProtocolError`, `DeckError`. Unhandled errors inside a pipeline task are converted to a single `error` protocol message and the session returns to `LISTENING`.

### 4.6 Configuration

`pydantic-settings` `Settings` class in `app/config.py` reads `.env`. Every setting has a type, default, and docstring. Provider selection: `STT_PROVIDER=groq|local`, `LLM_PROVIDER=groq|ollama`, `TTS_PROVIDER=kokoro`.

### 4.7 Tests

- `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`).
- Unit tests must not need network or API keys. Providers are faked via the protocol interfaces (`FakeLLM` yields a scripted token/tool stream).
- Integration tests are marked `@pytest.mark.integration` and skipped unless `GROQ_API_KEY` is set.
- Minimum unit coverage: `SentenceChunker`, `ConversationHistory.truncate`, `SlideController` (tool validation + keyword fallback), protocol parity, session state machine transitions including interrupt in each state. Pipeline modules ≥ 90 % lines, backend overall ≥ 80 %.
- Test names read as sentences and carry their catalogue ID in the docstring: `def test_interrupt_during_speaking_truncates_history(): """TC-BE-046."""`.
- Evals (`backend/evals/`) are separate from tests: they call real models, are opt-in, and their results are recorded in `docs/EVALS.md`. Add a dataset item whenever a real session misbehaves.

## 5. TypeScript standards (frontend)

- `strict: true`, `noUncheckedIndexedAccess: true`, no `any` (use `unknown` and narrow).
- ESLint with `@typescript-eslint/recommended-type-checked` and `react-hooks`; Prettier for formatting (100 cols, double quotes, trailing commas).
- Functional components with hooks; no class components. One component per file, named export matching the filename.
- State: a small `zustand` store for session state (`state`, `currentSlide`, `events`, `metrics`); no Redux.
- Audio code lives in `src/audio/` and has **no React imports**. It exposes plain classes (`AudioCapture`, `PlaybackQueue`, `VadController`) with explicit `start()` / `stop()` / `flush()` so they are unit-testable and leak-free.
- Every `AudioContext`, `MediaStream`, worklet node, and WebSocket has a matching teardown in the owning hook's cleanup. Starting and stopping the session five times must not leak (check `chrome://media-internals`).
- JSDoc on exported functions and classes; explain *why*, not *what*, in inline comments.
- No UI component library. CSS modules, CSS variables for theme, `prefers-reduced-motion` respected by the orb.

## 6. Repository hygiene

- `.gitignore` covers `.env`, `node_modules/`, `.venv/`, `__pycache__/`, `dist/`, model weights (`*.onnx`, `*.bin`) — Kokoro weights are downloaded at first run into `backend/models/` and never committed.
- `README.md` sections in order: what it is (with a GIF), quick start, architecture diagram, how barge-in works, measured latency numbers, eval summary, trade-offs and next steps, project structure.
- Keep `docs/PRD.md` current; mark shipped features with their milestone.
- Do not add dependencies without a one-line justification in the commit body. Prefer the standard library.

## 7. Definition of done for a feature

1. Behaviour matches the PRD's expected-behaviour table and acceptance criteria.
2. Its TC-xxx cases exist in `docs/TEST_CASES.md`, are implemented, and are marked `passing`.
3. `make lint` and `make test` pass.
4. Docstrings and types complete; no `TODO` left without an issue-style note (`TODO(hv): ...`).
5. Event log and metrics reflect the new behaviour where relevant.
6. `docs/ENGINEERING_LOG.md` has an entry; PRD/TRD updated if the change is user-visible or alters a decision.

## 8. Decision log

High-level decisions only; per-change reasoning lives in `docs/ENGINEERING_LOG.md`.

| Date | Decision | Why |
|---|---|---|
| 2026-09-09 | Open-weight STT→LLM→TTS pipeline instead of a speech-to-speech API | Barge-in and turn-taking are owned by this codebase and fully inspectable; zero cost; provider-agnostic. |
| 2026-09-09 | FastAPI backend, React + Vite frontend | Author preference; first-class WebSockets; lightweight. |
| 2026-09-09 | VAD in the browser, utterance-level STT | Instant client-side barge-in; one STT request per turn fits free-tier limits. |
| 2026-09-09 | Kokoro-82M via `kokoro-onnx` for TTS | Apache 2.0, CPU real-time, minimal dependencies, no account. |
| 2026-09-09 | No cloud deployment in v0.1.0 | Distributed as a runnable GitHub repository. |
| 2026-09-09 | Tests catalogued in `docs/TEST_CASES.md`, evals in `docs/EVALS.md`, reasoning in `docs/ENGINEERING_LOG.md` | Make intent and verification reviewable independently of the code. |
