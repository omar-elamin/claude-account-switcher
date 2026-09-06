"""Provider-independent usage state helpers."""

from dataclasses import dataclass


@dataclass(frozen=True)
class UsageWindow:
    label: str
    percent: float
    resets_in: str | None = None
    # A model-scoped limit (e.g. the Fable weekly window) is shown for
    # information only; it must not make auto-switch treat the account as
    # exhausted when the account's own 5h/7d windows still have room.
    scoped: bool = False


@dataclass(frozen=True)
class UsageState:
    available: bool
    display: str
    windows: tuple[UsageWindow, ...] = ()

    @property
    def max_percent(self) -> float | None:
        """Return the highest known utilization percentage."""
        return max((w.percent for w in self.windows if not w.scoped), default=None)

    def is_exhausted(self, threshold: float = 100.0) -> bool:
        """Return whether any known usage window has reached the threshold."""
        max_percent = self.max_percent
        return max_percent is not None and max_percent >= threshold
