"""Write a run's results twice: machine-readable and readable (TR-202).

The JSON is the record -- every item, every answer, every judge rationale -- so a number that looks
wrong can be traced to the answer that produced it. The Markdown is the summary that gets pasted
into ``docs/EVALS.md`` with the git SHA, which is what makes a claim in the README checkable.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .suites import SuiteResult

RESULTS = Path(__file__).resolve().parent / "results"
"""Where runs are written."""


def git_sha() -> str:
    """Read the current commit, so a result can be reproduced.

    Returns:
        The short SHA, or ``"unknown"`` outside a repository.
    """
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).resolve().parents[2],
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"
    return completed.stdout.strip() or "unknown"


def _format_metric(name: str, value: float) -> str:
    """Render one metric the way its units want to be read.

    Args:
        name: The metric's name, whose suffix says what it is.
        value: The measured value.

    Returns:
        A short string.
    """
    if name.endswith("_ms") or name.endswith("_ms_p50") or name.endswith("_ms_p95"):
        return f"{value:.0f} ms"
    if name in {"invalid_calls"}:
        return f"{value:.0f}"
    if name == "mean_score":
        return f"{value:.2f} / 2"
    return f"{value * 100:.1f} %"


def _format_threshold(name: str, comparison: str, value: float) -> str:
    """Render a threshold in the same units as its metric.

    Args:
        name: The metric's name.
        comparison: ``">="`` or ``"<="``.
        value: The bar.

    Returns:
        A short string.
    """
    return f"{comparison} {_format_metric(name, value)}"


def to_markdown(
    results: list[SuiteResult], *, model: str, judge_model: str, sha: str, stamp: str
) -> str:
    """Render the summary table recorded in ``docs/EVALS.md``.

    Args:
        results: Every suite that ran, in order.
        model: The model under evaluation.
        judge_model: The model that graded E2 and E3.
        sha: Git commit the run describes.
        stamp: ISO-8601 timestamp of the run.

    Returns:
        Markdown, ready to paste.
    """
    lines = [
        f"### {stamp[:10]} — {sha} — {model}",
        "",
        f"Models: LLM=`{model}` judge=`{judge_model}`",
        "",
        "| Suite | Metric | Value | Threshold | Pass |",
        "|---|---|---|---|---|",
    ]
    for result in results:
        for name, value in result.metrics.items():
            threshold = result.thresholds.get(name)
            bar = _format_threshold(name, *threshold) if threshold else "—"
            mark = "—" if threshold is None else ("yes" if result.meets(name) else "**no**")
            lines.append(
                f"| {result.suite} {result.title} | {name} | "
                f"{_format_metric(name, value)} | {bar} | {mark} |"
            )
    notes = [f"{result.suite}: {result.note}" for result in results if result.note]
    if notes:
        lines += ["", "Notes: " + " ".join(notes)]
    return "\n".join(lines)


def write(
    results: list[SuiteResult],
    *,
    model: str,
    judge_model: str,
    out: Path | None = None,
    sha: str | None = None,
) -> tuple[Path, Path]:
    """Write both files for one run.

    Args:
        results: Every suite that ran.
        model: The model under evaluation.
        judge_model: The model that graded.
        out: Path for the JSON; defaults to a timestamped file under ``results/``.
        sha: The commit the run describes. The runner reads it when the run starts; read
            here, at the end, a commit made during a forty-minute run would be recorded as
            the code that ran, which is how the 2026-09-11 judged run came to be stamped
            with a fix it never contained.

    Returns:
        The JSON path and the Markdown path.
    """
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    sha = sha or git_sha()
    RESULTS.mkdir(parents=True, exist_ok=True)
    json_path = out or RESULTS / f"{stamp}.json"
    markdown_path = json_path.with_suffix(".md")

    payload: dict[str, Any] = {
        "timestamp": stamp,
        "git_sha": sha,
        "model": model,
        "judge_model": judge_model,
        "suites": [asdict(result) | {"passed": result.passed} for result in results],
    }
    json_path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    markdown_path.write_text(
        to_markdown(results, model=model, judge_model=judge_model, sha=sha, stamp=stamp) + "\n",
        encoding="utf-8",
    )
    return json_path, markdown_path
