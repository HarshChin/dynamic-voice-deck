"""The LLM judge for the suites that cannot be scored by string comparison (TR-201).

Two of the six suites ask questions a program cannot answer: is this answer faithful to the notes,
and did this reply pretend to have said something it never said. Both are graded by the same model
family at ``temperature=0`` against a fixed rubric, and both are only believed when the judge has
first agreed with a hand-labelled calibration set on the same run.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.providers.base import LLMProvider, Message, TokenDelta

from .budget import BUDGET, NOT_ATTEMPTED

JUDGES = Path(__file__).resolve().parent / "judges"
"""Where the rubric prompts live."""

JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)
"""Matches the JSON object in a reply, tolerating a model that wraps it in prose or a fence."""


@dataclass(slots=True)
class Verdict:
    """One judged item.

    Attributes:
        payload: The parsed JSON the judge returned.
        raw: Everything it said, kept so a surprising score can be read back.
        error: Why the verdict is unusable, when it is.
    """

    payload: dict[str, Any]
    raw: str = ""
    error: str | None = None


def rubric(name: str) -> str:
    """Read one rubric prompt.

    Args:
        name: File stem under ``evals/judges``.

    Returns:
        The rubric text.
    """
    return (JUDGES / f"{name}.md").read_text(encoding="utf-8").strip()


async def judge(llm: LLMProvider, instructions: str, case: str) -> Verdict:
    """Ask the judge to score one item.

    Args:
        llm: The provider to judge with, expected to be at ``temperature=0``.
        instructions: The rubric, as the system message.
        case: The item to grade, as the user message.

    Returns:
        The verdict, with ``error`` set when the reply was not usable JSON, or when the day's
        budget was already spent and the judge was not asked (TR-205).
    """
    if BUDGET.exhausted:
        return Verdict(payload={}, error=NOT_ATTEMPTED)
    messages = [
        Message(role="system", content=instructions),
        Message(role="user", content=case),
    ]
    parts: list[str] = []
    try:
        async for event in llm.stream(messages, tools=[], tool_choice="none"):
            if isinstance(event, TokenDelta):
                parts.append(event.text)
    except Exception as exc:  # a failed judge is a recorded result, not a crashed run
        return Verdict(payload={}, error=f"{type(exc).__name__}: {exc}")

    raw = "".join(parts).strip()
    match = JSON_BLOCK.search(raw)
    if match is None:
        return Verdict(payload={}, raw=raw, error="no JSON object in the reply")
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return Verdict(payload={}, raw=raw, error=f"unparseable JSON: {exc}")
    if not isinstance(payload, dict):
        return Verdict(payload={}, raw=raw, error="JSON was not an object")
    return Verdict(payload=payload, raw=raw)
