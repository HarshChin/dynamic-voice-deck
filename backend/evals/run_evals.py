"""Run the eval suites against a real model (TR-200, TR-204).

    uv run python -m evals.run_evals --suite all --model qwen/qwen3.8-27b

Opt-in, because every item is a real model call and the free tier is the binding constraint: a full
run is about seventy-five of them, roughly 170,000 tokens against a daily budget of 200,000 per
model, and E1 on its own is about thirty calls. Calls are paced by default so the per-minute limiter
never refuses one, and the run stops attempting items once the daily budget does (TR-205). Results
land in ``evals/results/`` as JSON and as a Markdown summary to paste into ``docs/EVALS.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from app.config import Settings, get_settings
from app.logging_setup import configure_logging
from app.pipeline.prompt import PromptBuilder
from app.providers.base import LLMProvider

from . import harness, suites
from .budget import BUDGET
from .harness import build_llm, build_tts, load_deck
from .pacing import PacedLLM, Pacer
from .report import write
from .suites import Context, SuiteResult

SUITES = ("E1", "E2", "E3", "E4", "E5", "E6")
"""Every suite, in the order they are run."""

JUDGED = ("E2", "E3")
"""Suites whose numbers depend on the judge, and are therefore gated on its calibration."""

DEFAULT_MIN_INTERVAL_S = 31.0
"""Spacing between hosted-model calls unless the command line says otherwise.

Two ~2,800-token calls fit a 7,000-token minute; a third is refused, and a refused request counts
against the minute that refused it. Local models are not paced: there is no limiter to respect.
"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Read the command line.

    Args:
        argv: Arguments to parse; defaults to the process's own.

    Returns:
        The parsed arguments.
    """
    parser = argparse.ArgumentParser(prog="run_evals", description=__doc__)
    parser.add_argument(
        "--suite",
        default="all",
        help=f"which suites to run: all, or a comma-separated list of {', '.join(SUITES)}",
    )
    parser.add_argument("--model", default=None, help="model id to evaluate; defaults to .env")
    parser.add_argument(
        "--provider",
        default="groq",
        choices=("groq", "ollama"),
        help="where the model runs; `ollama` measures the local fallback (TR-085)",
    )
    parser.add_argument("--judge-model", default=None, help="model id to grade with")
    parser.add_argument("--out", type=Path, default=None, help="path for the JSON result")
    parser.add_argument(
        "--max-wait",
        type=float,
        default=None,
        help="longest to wait out one rate limit, in seconds; a longer wait is read as the daily "
        "budget being spent, and the run stops attempting items (default 300)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="items in flight at once; 1 when the budget is the bottleneck (default 2)",
    )
    parser.add_argument(
        "--min-interval",
        type=float,
        default=None,
        help="seconds between model calls, shared by subject and judge; 31 keeps a 7,000-token "
        "minute from ever refusing a ~2,800-token call (default 31 for groq, 0 for ollama)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="evaluate only the first N items of each dataset, for a cheap smoke run",
    )
    return parser.parse_args(argv)


def _apply_run_options(args: argparse.Namespace) -> None:
    """Push the pacing options into the modules that read them.

    Module state rather than parameters threaded through every suite, because
    these exist only to make a run cheaper or more patient, never to change what
    it measures.

    Args:
        args: Parsed command line.
    """
    harness.LIMIT = args.limit
    if args.max_wait is not None:
        harness.MAX_RATE_LIMIT_WAIT_S = args.max_wait
    if args.concurrency is not None:
        suites.MAX_CONCURRENCY = args.concurrency


def _build_models(
    args: argparse.Namespace, settings: Settings, model: str, judge_model: str
) -> tuple[LLMProvider, LLMProvider]:
    """Build the subject model and the judge, paced together when the provider needs it.

    Args:
        args: Parsed command line.
        settings: Loaded settings, supplying credentials and base URLs.
        model: The model under evaluation.
        judge_model: The model doing the grading.

    Returns:
        The subject provider and the judge provider.
    """
    llm = build_llm(settings, model, provider=args.provider)
    # Zero, because a rubric graded differently on two runs is not a measurement (TR-201).
    judge_llm = build_llm(settings, judge_model, provider=args.provider, temperature=0.0)
    interval = args.min_interval
    if interval is None:
        interval = DEFAULT_MIN_INTERVAL_S if args.provider == "groq" else 0.0
    if interval > 0:
        # One pacer for both: they draw on the same account, and the account's minute is shared.
        pacer = Pacer(interval)
        return PacedLLM(llm, pacer), PacedLLM(judge_llm, pacer)
    return llm, judge_llm


def _exit_status(results: list[SuiteResult]) -> int:
    """Decide the exit status, and say why on stderr.

    Args:
        results: Every suite that ran.

    Returns:
        0 only when every threshold was met and every item was attempted.
    """
    failed = [result.suite for result in results if not result.passed]
    if failed:
        print(f"below threshold: {', '.join(failed)}", file=sys.stderr)
    if BUDGET.exhausted:
        print(
            f"the daily budget ran out during the run: the provider asked for a "
            f"{BUDGET.exhausted_after_s:.0f} s wait, and every item after that was not attempted. "
            "The numbers above cover the items that were. Run again once the bucket has refilled; "
            "it does so at about 2.3 tokens a second, so a full day is a full day.",
            file=sys.stderr,
        )
        return 1
    return 1 if failed else 0


async def run(args: argparse.Namespace) -> int:
    """Run the selected suites and write the results.

    Args:
        args: Parsed command line.

    Returns:
        Process exit status: 0 when every threshold was met.
    """
    settings = get_settings()
    _apply_run_options(args)
    model = args.model or (
        settings.ollama_model if args.provider == "ollama" else settings.groq_llm_model
    )
    judge_model = args.judge_model or model
    wanted = (
        SUITES
        if args.suite == "all"
        else tuple(name.strip().upper() for name in args.suite.split(",") if name.strip())
    )
    unknown = [name for name in wanted if name not in SUITES]
    if unknown:
        print(f"unknown suite: {', '.join(unknown)}", file=sys.stderr)
        return 2

    deck = load_deck()
    prompts = PromptBuilder()
    llm, judge_llm = _build_models(args, settings, model, judge_model)
    results: list[SuiteResult] = []

    needs_judge = any(name in JUDGED for name in wanted)
    if needs_judge:
        context = Context(
            deck=deck,
            llm=llm,
            judge_llm=judge_llm,
            tts=build_tts(settings, real=False),
            prompts=prompts,
        )
        calibration = await suites.calibrate(context)
        results.append(calibration)
        if not calibration.passed:
            print(
                f"judge agreement {calibration.metrics['agreement']:.0%} is below "
                f"{suites.CALIBRATION_THRESHOLD:.0%}; the judged suites will be reported but "
                "should not be believed",
                file=sys.stderr,
            )

    fake_context = Context(
        deck=deck,
        llm=llm,
        judge_llm=judge_llm,
        tts=build_tts(settings, real=False),
        prompts=prompts,
    )

    routing: SuiteResult | None = None
    if "E1" in wanted or "E6" in wanted:
        routing = await suites.e1_routing(fake_context)
        if "E1" in wanted:
            results.append(routing)
    if "E2" in wanted:
        results.append(await suites.e2_interruption(fake_context))
    if "E3" in wanted:
        results.append(await suites.e3_grounded(fake_context))
    if "E4" in wanted:
        results.append(suites.e4_style([r for r in results if r.suite in {"E1", "E2", "E3"}]))
    if "E5" in wanted:
        real_context = Context(
            deck=deck,
            llm=llm,
            judge_llm=judge_llm,
            tts=build_tts(settings, real=True),
            prompts=prompts,
        )
        await real_context.tts.warm_up()
        results.append(await suites.e5_latency(real_context))
    if "E6" in wanted and routing is not None:
        results.append(suites.e6_tools(routing))

    json_path, markdown_path = write(results, model=model, judge_model=judge_model, out=args.out)
    print(markdown_path.read_text(encoding="utf-8"))
    print(f"wrote {json_path}\n      {markdown_path}")
    return _exit_status(results)


def main() -> int:
    """Entry point.

    Returns:
        Process exit status.
    """
    # The suites print their own results, and one line per HTTP request would bury them.
    configure_logging(get_settings().model_copy(update={"log_level": "WARNING"}))
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
