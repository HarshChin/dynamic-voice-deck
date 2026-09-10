"""Tool definitions offered to the model (PRD F5, TR-072).

These schemas are the agent's only way to move the deck. They are deliberately
small: one tool to navigate, one to emphasise a point. Every argument the model
sends is validated against the loaded deck before it takes effect
(:mod:`app.pipeline.slides`), so a hallucinated slide number cannot desynchronise
the UI.
"""

from __future__ import annotations

from ..decks.models import MAX_SLIDES
from ..providers.base import ToolSpec

GO_TO_SLIDE = "go_to_slide"
"""Name of the navigation tool."""

HIGHLIGHT_BULLET = "highlight_bullet"
"""Name of the emphasis tool."""


def build_tools(slide_count: int) -> list[ToolSpec]:
    """Build the tool specs for a deck of a given size.

    The upper bound is baked into the schema rather than left to validation
    alone, because telling the model the real range up front is far more
    effective than rejecting a bad call afterwards.

    Args:
        slide_count: Number of slides in the deck being presented.

    Returns:
        The tools to offer the model.
    """
    upper = max(1, min(slide_count, MAX_SLIDES))
    return [
        ToolSpec(
            name=GO_TO_SLIDE,
            description=(
                "Navigate the deck to the slide that best answers the user's question, "
                "or to the next slide when continuing a walkthrough. Call this BEFORE "
                "speaking about that slide's content, so the audience sees what you are "
                "describing. Do not call it when the current slide already answers the "
                "question, and do not call it for questions unrelated to the deck."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "slide_index": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": upper,
                        "description": f"Target slide, from 1 to {upper}.",
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "One short clause shown in the UI explaining the jump, "
                            "e.g. 'User asked about interruption handling'."
                        ),
                    },
                },
                "required": ["slide_index", "reason"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name=HIGHLIGHT_BULLET,
            description=(
                "Emphasise one bullet on the CURRENT slide while you talk about it. "
                "Optional; use it when your answer is about a specific point rather "
                "than the whole slide."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "bullet_index": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Zero-based index of the bullet on the current slide.",
                    }
                },
                "required": ["bullet_index"],
                "additionalProperties": False,
            },
        ),
    ]
