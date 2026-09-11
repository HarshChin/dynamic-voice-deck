"""What a run has learned about the account's daily budget, shared by everything that calls a model.

Groq's daily limit is a bucket of 200,000 tokens per model that refills continuously, at about 2.3
tokens a second -- read off the rate-limit headers, where 72 requests used showed a reset of
1 h 43 m 40 s, which is 72 times 86.4 seconds. Once the bucket is empty a ~2,800-token call is
refused with a wait of twenty minutes or so, and the refused request appears to be charged like a
served one, so from that point every further attempt is at best one answer per twenty minutes and at
worst pure waste. A run therefore stops attempting items the moment the day is declared spent and
records the rest as not attempted, never as wrong (TR-205).
"""

from __future__ import annotations

from dataclasses import dataclass

NOT_ATTEMPTED = "ProviderError: not attempted; the daily budget ran out earlier in this run"
"""The error recorded on every item skipped once the budget was declared spent.

Prefixed like a real provider failure so the suites exclude it from their denominators the same
way, and distinctive enough that a note can count it separately.
"""


@dataclass(slots=True)
class Budget:
    """Whether the account's daily budget is known to be spent.

    Attributes:
        exhausted_after_s: The wait the provider asked for when it refused the call that spent
            the day, or ``None`` while the budget is believed to be available.
    """

    exhausted_after_s: float | None = None

    @property
    def exhausted(self) -> bool:
        """Whether a call has been refused for the day's budget."""
        return self.exhausted_after_s is not None

    def spend(self, wait_s: float) -> None:
        """Record that the provider declared the day spent.

        Args:
            wait_s: The advertised wait, kept for the run's report. The first is kept.
        """
        if self.exhausted_after_s is None:
            self.exhausted_after_s = wait_s

    def reset(self) -> None:
        """Forget a spent budget; a new run starts optimistic."""
        self.exhausted_after_s = None


BUDGET = Budget()
"""The one budget a run draws on. Module state because the subject model and the judge share it."""
