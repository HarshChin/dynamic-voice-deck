"""Run the eval suites against a real model (TR-200, TR-204).

    uv run python -m evals.run_evals --suite all --model qwen/qwen3.8-27b

Opt-in, because every item is a real model call and the free tier is the binding constraint: a full
run is roughly a hundred of them. Results land in ``evals/results/`` as JSON and as a Markdown
summary to paste into ``docs/EVALS.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from app.config import get_settings
from app.logging_setup import configure_logging
from app.pipeline.prompt import PromptBuilder

from . import harness, suites
from .harness import build_llm, build_tts, load_deck
from .report import write
from .suites import Context, SuiteResult

SUITES = ("E1", "E2", "E3", "E4", "E5", "E6")
"""Every suite, in the order they are run."""

JUDGED = ("E2", "E3")
"""Suites whose numbers depend on the judge, and are therefore gated on its calibration."""


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
    parser.add_argument("--judge-model", default=None, help="model id to grade with")
    parser.add_argument("--out", type=Path, default=None, help="path for the JSON result")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="evaluate only the first N items of each dataset, for a cheap smoke run",
    )
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    """Run the selected suites and write the results.

    Args:
        args: Parsed command line.

    Returns:
        Process exit status: 0 when every threshold was met.
    """
    settings = get_settings()
    harness.LIMIT = args.limit
    model = args.model or settings.groq_llm_model
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
    llm = build_llm(settings, model)
    # Zero, because a rubric graded differently on two runs is not a measurement (TR-201).
    judge_llm = build_llm(settings, judge_model, temperature=0.0)
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
    failed = [result.suite for result in results if not result.passed]
    if failed:
        print(f"below threshold: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


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
