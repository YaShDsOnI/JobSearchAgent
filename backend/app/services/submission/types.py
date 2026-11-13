"""Runner interface types shared by the service and the browser runner (no browser imports)."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class AbortBeforeClick(Exception):
    """Raised by the before-click hook when a re-check fails; the click never happens."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class RunRequest:
    url: str
    origin: str
    answers: dict[str, dict[str, Any]]  # question_key -> {"value", "provenance"} (answered only)
    resume_path: Path
    resume_sha256: str


@dataclass
class FillResult:
    pause_reason: str | None = None
    manifest: dict[str, Any] | None = None
    fill_hash: str | None = None


@dataclass
class SubmitResult:
    outcome: str  # "paused" | "aborted" | "verified" | "unverified"
    reason: str | None = None
    confirmation_id: str | None = None
    clicked: bool = False
    manifest: dict[str, Any] | None = None


class Runner(Protocol):
    def fill(self, request: RunRequest) -> FillResult: ...

    def submit(
        self,
        request: RunRequest,
        *,
        expected_fill_hash: str,
        before_click: Callable[[], None],
    ) -> SubmitResult: ...
